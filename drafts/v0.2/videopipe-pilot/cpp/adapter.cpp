// Real VideoPipe source -> scenario adapter -> sink integration.
// The VLSP interpreter and inference services reside in the Python worker.
#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <future>
#include <iomanip>
#include <iostream>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include "json.hpp"
#include "nodes/vp_src_node.h"

using json = nlohmann::json;
using Meta = std::shared_ptr<vp_objects::vp_meta>;

static std::string thread_id() {
    std::ostringstream out; out << std::this_thread::get_id(); return out.str();
}

class Worker {
    pid_t pid_ = -1;
    FILE* input_ = nullptr;
    FILE* output_ = nullptr;
    bool closed_ = false;
    int exit_code_ = 0;
public:
    Worker(const std::string& python, const std::string& script) {
        int input_pipe[2], output_pipe[2];
        if (pipe(input_pipe) || pipe(output_pipe)) throw std::runtime_error("pipe failed");
        pid_ = fork();
        if (pid_ < 0) throw std::runtime_error("fork failed");
        if (pid_ == 0) {
            dup2(input_pipe[0], STDIN_FILENO);
            dup2(output_pipe[1], STDOUT_FILENO);
            close(input_pipe[0]); close(input_pipe[1]);
            close(output_pipe[0]); close(output_pipe[1]);
            execl(python.c_str(), python.c_str(), "-u", script.c_str(), static_cast<char*>(nullptr));
            std::fprintf(stderr, "worker exec failed: %s\n", std::strerror(errno));
            _exit(127);
        }
        close(input_pipe[0]); close(output_pipe[1]);
        input_ = fdopen(input_pipe[1], "w");
        output_ = fdopen(output_pipe[0], "r");
        if (!input_ || !output_) throw std::runtime_error("fdopen failed");
        setvbuf(input_, nullptr, _IOLBF, 0);
    }
    ~Worker() { close_and_wait(); }
    json request(const json& request) {
        const auto wire = request.dump();
        if (std::fwrite(wire.data(), 1, wire.size(), input_) != wire.size() ||
            std::fputc('\n', input_) == EOF || std::fflush(input_))
            throw std::runtime_error("worker write failed");
        char* line = nullptr; size_t capacity = 0;
        const auto count = getline(&line, &capacity, output_);
        if (count < 0) { std::free(line); throw std::runtime_error("worker closed stdout before response"); }
        std::string response(line, static_cast<size_t>(count)); std::free(line);
        auto parsed = json::parse(response);
        if (!parsed.is_object() || parsed.value("id", json()) != request.value("id", json()))
            throw std::runtime_error("worker response id mismatch");
        return parsed;
    }
    int close_and_wait() noexcept {
        if (closed_) return exit_code_;
        closed_ = true;
        if (input_) { std::fclose(input_); input_ = nullptr; }
        if (output_) { std::fclose(output_); output_ = nullptr; }
        if (pid_ > 0) {
            int status = 0;
            pid_t waited;
            do { waited = waitpid(pid_, &status, 0); } while (waited < 0 && errno == EINTR);
            if (waited < 0) {
                exit_code_ = 125;
                std::fprintf(stderr, "worker waitpid failed: %s\n", std::strerror(errno));
            } else if (WIFEXITED(status)) {
                exit_code_ = WEXITSTATUS(status);
                if (exit_code_) std::fprintf(stderr, "worker exited with code %d\n", exit_code_);
            } else if (WIFSIGNALED(status)) {
                exit_code_ = 128 + WTERMSIG(status);
                std::fprintf(stderr, "worker killed by signal %d\n", WTERMSIG(status));
            } else {
                exit_code_ = 125;
                std::fprintf(stderr, "worker ended with unexpected wait status %d\n", status);
            }
        }
        return exit_code_;
    }
};

struct Packet {
    json request;
    json response;
    json source_error;
    std::promise<json> completed;
    std::string source_thread, adapter_thread, sink_thread;
    size_t frame_bytes = 0;
    std::string frame_hash;
};

struct PacketCarrier {
    std::shared_ptr<Packet> packet;
    explicit PacketCarrier(std::shared_ptr<Packet> packet): packet(std::move(packet)) {}
    virtual ~PacketCarrier() = default;
};

class RequestFrame final: public vp_objects::vp_frame_meta, public PacketCarrier {
public:
    RequestFrame(cv::Mat frame, int frame_index, std::shared_ptr<Packet> packet):
        vp_frame_meta(frame, frame_index, 0, frame.cols, frame.rows, 25), PacketCarrier(std::move(packet)) {}
    std::shared_ptr<vp_objects::vp_meta> clone() override { return std::make_shared<RequestFrame>(*this); }
};

class RequestControl final: public vp_objects::vp_control_meta, public PacketCarrier {
public:
    explicit RequestControl(std::shared_ptr<Packet> packet):
        vp_control_meta(vp_objects::vp_control_type::SPEAK, 0, packet->request.value("id", json()).dump()),
        PacketCarrier(std::move(packet)) {}
    std::shared_ptr<vp_objects::vp_meta> clone() override { return std::make_shared<RequestControl>(*this); }
};

static std::shared_ptr<Packet> packet_of(const Meta& meta) {
    auto carrier = std::dynamic_pointer_cast<PacketCarrier>(meta);
    if (!carrier) throw std::runtime_error("non-request meta in pilot graph");
    return carrier->packet;
}

static int hex_value(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    throw std::runtime_error("invalid image hex character");
}

static cv::Mat materialize(const json& image) {
    const int width = image.at("width").get<int>();
    const int height = image.at("height").get<int>();
    if (width <= 0 || height <= 0 || width > 16384 || height > 16384)
        throw std::runtime_error("invalid image dimensions");
    const auto& hex = image.at("bgr_hex").get_ref<const std::string&>();
    const size_t bytes = static_cast<size_t>(width) * static_cast<size_t>(height) * 3;
    if (hex.size() != bytes * 2) throw std::runtime_error("image hex size mismatch");
    cv::Mat frame(height, width, CV_8UC3);
    for (size_t i = 0; i < bytes; ++i) frame.data[i] = (hex_value(hex[2*i]) << 4) | hex_value(hex[2*i+1]);
    return frame;
}

static std::string repack(const cv::Mat& frame, std::shared_ptr<Packet> packet) {
    static const char* digits = "0123456789abcdef";
    std::string hex;
    hex.reserve(frame.total() * frame.elemSize() * 2);
    uint64_t hash = 14695981039346656037ull;
    for (int row = 0; row < frame.rows; ++row) {
        const auto* bytes = frame.ptr<unsigned char>(row);
        for (size_t i = 0; i < static_cast<size_t>(frame.cols)*frame.elemSize(); ++i) {
            hex += digits[bytes[i] >> 4]; hex += digits[bytes[i] & 15];
            hash ^= bytes[i]; hash *= 1099511628211ull;
        }
    }
    std::ostringstream digest; digest << std::hex << std::setfill('0') << std::setw(16) << hash;
    packet->frame_hash = digest.str(); packet->frame_bytes = frame.total() * frame.elemSize();
    return hex;
}

class Evidence {
    std::mutex lock_;
    json hooks_ = json::object();
public:
    std::atomic<size_t> source_seen{0}, adapter_seen{0}, sink_seen{0};
    void hook(const std::string& node, const std::string& phase, int depth) {
        std::lock_guard<std::mutex> guard(lock_);
        auto& item = hooks_[node][phase];
        if (!item.is_object()) item = {{"count", 0}, {"max_queue_depth", 0}};
        item["count"] = item["count"].get<size_t>() + 1;
        item["max_queue_depth"] = std::max(item["max_queue_depth"].get<int>(), depth);
    }
    json transport(const Packet& packet, const Meta& meta) {
        std::lock_guard<std::mutex> guard(lock_);
        json result = {{"engine", "VideoPipe"}, {"upstream_commit", VP_UPSTREAM_COMMIT},
          {"integration", "source -> VLSP worker adapter -> sink"},
          {"meta_type", meta->meta_type == vp_objects::vp_meta_type::FRAME ? "FRAME" : "CONTROL"},
          {"source_seen", source_seen.load()}, {"adapter_seen", adapter_seen.load()}, {"sink_seen", sink_seen.load()},
          {"threads", {{"source", packet.source_thread}, {"adapter", packet.adapter_thread}, {"sink", packet.sink_thread}}},
          {"frame_bytes", packet.frame_bytes}, {"frame_fnv1a64", packet.frame_hash}, {"hooks", hooks_}};
        if (auto frame = std::dynamic_pointer_cast<RequestFrame>(meta)) {
            const auto context = packet.request.value("context", json::object());
            json logical = json::object();
            for (const auto* key: {"source_id", "run_id", "sequence", "event_time_ms"})
                logical[key] = context.is_object() ? context.value(key, json()) : json();
            logical["frame_id"] = nullptr;
            if (packet.request.contains("inputs") && packet.request["inputs"].is_object()) {
                const auto& inputs = packet.request["inputs"];
                if (inputs.contains("frame") && inputs["frame"].is_object())
                    logical["frame_id"] = inputs["frame"].value("frame_id", json());
            }
            result["native_frame_index"] = frame->frame_index;
            result["native_channel_index"] = frame->channel_index;
            result["logical"] = std::move(logical);
            result["identity_mapping"] = "logical identity is carried in the host ingress envelope; native frame_index is a transport counter, not logical sequence or UTC PTS";
            result["ingress_trust"] = "local pilot harness; authentication is outside this adapter";
            result["channel_mapping"] = "native channel 0 multiplexes logical cameras; native camera ingest is outside this pilot";
        }
        return result;
    }
    void attach(const std::shared_ptr<vp_nodes::vp_node>& node) {
        node->set_meta_arriving_hooker([this](auto name, auto depth, auto) { hook(name, "arriving", depth); });
        node->set_meta_handling_hooker([this](auto name, auto depth, auto) { hook(name, "handling", depth); });
        node->set_meta_handled_hooker([this](auto name, auto depth, auto) { hook(name, "handled", depth); });
        node->set_meta_leaving_hooker([this](auto name, auto depth, auto) { hook(name, "leaving", depth); });
    }
};

class RequestSource final: public vp_nodes::vp_src_node {
    std::mutex requests_lock_;
    std::condition_variable available_;
    std::deque<std::shared_ptr<Packet>> requests_;
    bool stopping_ = false, joined_ = false;
    Evidence& evidence_;
protected:
    void handle_run() override {
        gate.knock();
        while (true) {
            std::shared_ptr<Packet> packet;
            {
                std::unique_lock<std::mutex> guard(requests_lock_);
                available_.wait(guard, [this] { return stopping_ || !requests_.empty(); });
                if (requests_.empty() && stopping_) break;
                packet = requests_.front(); requests_.pop_front();
            }
            packet->source_thread = thread_id(); ++evidence_.source_seen;
            Meta meta;
            const auto op = packet->request.value("op", "");
            if (op == "process" || op == "enqueue") {
                try { meta = std::make_shared<RequestFrame>(materialize(packet->request.at("image")), ++frame_index, packet); }
                catch (const std::exception& e) {
                    packet->source_error = {{"code", "videopipe_image_materialization"}, {"message", e.what()}};
                    meta = std::make_shared<RequestControl>(packet);
                }
            } else meta = std::make_shared<RequestControl>(packet);
            pendding_meta(meta);
        }
        out_queue.push(nullptr); out_queue_semaphore.signal();
    }
public:
    explicit RequestSource(Evidence& evidence): vp_src_node("request_source", 0), evidence_(evidence) { initialized(); }
    ~RequestSource() { shutdown(); }
    std::future<json> submit(json request) {
        auto packet = std::make_shared<Packet>(); packet->request = std::move(request);
        auto future = packet->completed.get_future();
        { std::lock_guard<std::mutex> guard(requests_lock_); requests_.push_back(packet); }
        available_.notify_one(); return future;
    }
    void shutdown() {
        if (joined_) return;
        joined_ = true;
        { std::lock_guard<std::mutex> guard(requests_lock_); stopping_ = true; }
        available_.notify_all(); vp_src_node::deinitialized();
    }
};

class ScenarioAdapter final: public vp_nodes::vp_node {
    Worker& worker_;
    Evidence& evidence_;
    Meta dispatch(const Meta& meta) {
        auto packet = packet_of(meta);
        packet->adapter_thread = thread_id(); ++evidence_.adapter_seen;
        try {
            if (!packet->source_error.is_null()) throw std::runtime_error(packet->source_error.dump());
            auto request = packet->request;
            if (auto frame = std::dynamic_pointer_cast<RequestFrame>(meta)) {
                request["image"]["width"] = frame->frame.cols;
                request["image"]["height"] = frame->frame.rows;
                request["image"]["bgr_hex"] = repack(frame->frame, packet);
            }
            packet->response = worker_.request(request);
        } catch (const std::exception& e) {
            packet->response = {{"id", packet->request.value("id", json())}, {"ok", false},
              {"error", {{"code", "videopipe_transport_error"}, {"message", e.what()}}}};
        }
        return meta;
    }
protected:
    Meta handle_frame_meta(std::shared_ptr<vp_objects::vp_frame_meta> meta) override { return dispatch(meta); }
    Meta handle_control_meta(std::shared_ptr<vp_objects::vp_control_meta> meta) override { return dispatch(meta); }
public:
    ScenarioAdapter(Worker& worker, Evidence& evidence): vp_node("vlsp_adapter"), worker_(worker), evidence_(evidence) { initialized(); }
    ~ScenarioAdapter() { deinitialized(); }
};

class ResponseSink final: public vp_nodes::vp_node {
    Evidence& evidence_;
    Meta finish(const Meta& meta) {
        auto packet = packet_of(meta);
        packet->sink_thread = thread_id(); ++evidence_.sink_seen;
        auto response = packet->response;
        response["transport"] = evidence_.transport(*packet, meta);
        packet->completed.set_value(std::move(response));
        return nullptr;
    }
protected:
    Meta handle_frame_meta(std::shared_ptr<vp_objects::vp_frame_meta> meta) override { return finish(meta); }
    Meta handle_control_meta(std::shared_ptr<vp_objects::vp_control_meta> meta) override { return finish(meta); }
public:
    explicit ResponseSink(Evidence& evidence): vp_node("response_sink"), evidence_(evidence) { initialized(); }
    vp_nodes::vp_node_type node_type() override { return vp_nodes::vp_node_type::DES; }
    ~ResponseSink() { deinitialized(); }
};

int main(int argc, char** argv) {
    std::signal(SIGPIPE, SIG_IGN);
    std::string python, worker_path;
    for (int i = 1; i < argc; ++i) {
        const std::string arg(argv[i]);
        if (arg == "--python" && i+1 < argc) python = argv[++i];
        else if (arg == "--worker" && i+1 < argc) worker_path = argv[++i];
        else { std::cerr << "usage: vlsp_videopipe --python /abs/python --worker /abs/worker.py\n"; return 2; }
    }
    if (python.empty() || worker_path.empty()) { std::cerr << "--python and --worker are required\n"; return 2; }
    try {
        Worker worker(python, worker_path); // fork before VideoPipe starts its threads
        auto& logger = vp_utils::vp_logger::get_logger();
        logger.log_level = vp_utils::vp_log_level::ERROR;
        logger.log_to_console = false; logger.log_to_file = false;
        logger.log_dir = (std::filesystem::path(argv[0]).parent_path() / "logs").string();
        logger.init();
        Evidence evidence;
        auto source = std::make_shared<RequestSource>(evidence);
        auto adapter = std::make_shared<ScenarioAdapter>(worker, evidence);
        auto sink = std::make_shared<ResponseSink>(evidence);
        evidence.attach(source); evidence.attach(adapter); evidence.attach(sink);
        adapter->attach_to({source}); sink->attach_to({adapter}); source->start();
        std::string line;
        while (std::getline(std::cin, line)) {
            if (line.empty()) continue;
            auto request = json::parse(line);
            const bool shutdown = request.value("op", "") == "shutdown";
            auto completed = source->submit(std::move(request));
            // One command in flight. This future is the source/adapter/sink barrier.
            auto response = completed.get();
            std::cout << response.dump() << std::endl;
            // A busy worker may reject shutdown while frames are still in flight.
            // Keep the graph and its worker alive until shutdown is acknowledged.
            if (shutdown && response.value("ok", false)) break;
        }
        source->shutdown();
        source->detach_recursively();
        sink.reset(); adapter.reset(); source.reset();
        return worker.close_and_wait();
    } catch (const std::exception& e) { std::cerr << "adapter fatal: " << e.what() << '\n'; return 1; }
    return 0;
}
