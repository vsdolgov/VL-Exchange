"""Write current build provenance only after a matching successful real smoke."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess

ROOT = Path(__file__).resolve().parents[1]
VP = ROOT / "vendor/VideoPipe"
CPP = ROOT / "cpp"
EXPECTED_VP_COMMIT = "2a3b461e555725681ec2c0b3693e743e10408299"
EXPECTED_OCV_SHA = "44c106d5bb47efec04e531fd93008b3fcd1d27138985c5baf4eafac0e1ec9e9d"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_output(*args):
    return subprocess.check_output(args, text=True).strip()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def tree_fingerprint(tree):
    """SHA256 of sorted relative UTF-8 name + NUL + file SHA256 ASCII + NUL."""
    digest = hashlib.sha256()
    count = 0
    for path in sorted(tree.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(tree).as_posix().encode("utf-8") + b"\0")
            digest.update(sha(path).encode("ascii") + b"\0")
            count += 1
    return {"sha256": digest.hexdigest(), "files": count,
            "algorithm": "sorted relative UTF-8 path + NUL + SHA256 hex of file + NUL"}


def main():
    commit = checked_output("git", "-C", str(VP), "rev-parse", "HEAD")
    require(commit == EXPECTED_VP_COMMIT, "Unexpected VideoPipe commit")
    tracked_diff = checked_output("git", "-C", str(VP), "diff", "HEAD", "--stat")
    require(not tracked_diff, "Tracked VideoPipe source differs from the pinned commit")
    archive_sha = sha(ROOT / "vendor/opencv-4.12.0.tar.gz")
    require(archive_sha == EXPECTED_OCV_SHA, "Unexpected OpenCV archive hash")
    binary_paths = ["build/adapter/vlsp_videopipe", "build/adapter/libvideopipe_headless.a",
                    "build/opencv-core/lib/libopencv_core.a"]
    binaries = {name: sha(ROOT / name) for name in binary_paths}
    smoke_path = ROOT / "build/transport-smoke.json"
    smoke = json.loads(smoke_path.read_text())
    require(smoke.get("ok") is True, "Transport smoke did not pass")
    require(smoke.get("normal_exit_code") == 0, "Transport smoke did not verify normal exit")
    require(smoke.get("binary_sha256") == binaries[binary_paths[0]], "Smoke is stale for the current binary")
    expected_tests = ["cpp/smoke_transport.py", "cpp/echo_worker_test.py"]
    require(smoke.get("test_sources_sha256") == {name: sha(ROOT / name) for name in expected_tests},
            "Smoke is stale for the current smoke test sources")
    responses = smoke.get("responses", [])
    require(smoke.get("commands") == len(responses) == 7, "Expected seven real smoke responses")
    for number, response in enumerate(responses, 1):
        require(response.get("ok") is True, "A smoke response failed")
        transport = response.get("transport", {})
        require(transport.get("upstream_commit") == commit, "Smoke used another upstream build")
        require(all(transport.get(key) == number for key in ("source_seen", "adapter_seen", "sink_seen")),
                "Smoke runtime counters disagree")
    abnormal = smoke.get("abnormal_worker_exit", {})
    require(abnormal.get("adapter_exit_code") == 7 and abnormal.get("shutdown_ack_received") is True,
            "Smoke did not verify abnormal worker status after shutdown ack")
    refused = smoke.get("rejected_shutdown", {})
    require(refused.get("exit_code") == 0 and
            [response.get("ok") for response in refused.get("responses", [])] == [False, True, True] and
            [response.get("echo", {}).get("op") for response in refused.get("responses", [])] == ["shutdown", "snapshot", "shutdown"],
            "Smoke did not verify graph remains active after rejected shutdown")
    cache = ROOT / "build/adapter/CMakeCache.txt"
    cache_values = {}
    for line in cache.read_text().splitlines():
        if line and not line.startswith(("#", "//")) and ":" in line and "=" in line:
            key, value = line.split("=", 1)
            cache_values[key.split(":", 1)[0]] = value
    compiler = cache_values["CMAKE_CXX_COMPILER"]
    manifest = {
        "kind": "real-VideoPipe-runtime integration adapter; Python VLSP interpreter shared with baseline",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "compiler": checked_output(compiler, "--version").splitlines()[0],
        "compiler_path": compiler,
        "cmake": checked_output("cmake", "--version").splitlines()[0],
        "videopipe": {"url": "https://github.com/sherlockchou86/VideoPipe", "commit": commit,
            "tracked_diff": tracked_diff,
            "upstream_cpp_sha256": {path.relative_to(VP).as_posix(): sha(path)
                for folder in ("nodes", "objects", "utils") for path in sorted((VP / folder).rglob("*.cpp"))}},
        "opencv": {"version": "4.12.0", "archive_sha256": archive_sha, "built_modules": ["core"],
            "source": "https://github.com/opencv/opencv/archive/refs/tags/4.12.0.tar.gz",
            "extracted_source_tree": tree_fingerprint(ROOT / "vendor/opencv-4.12.0")},
        "adapter_sources": {path.relative_to(CPP).as_posix(): sha(path)
            for path in sorted(CPP.rglob("*")) if path.is_file() and "__pycache__" not in path.parts},
        "binaries": binaries,
        "build_configuration": {"cmake_cache_sha256": sha(cache),
            "build_type": cache_values.get("CMAKE_BUILD_TYPE"),
            "native_flags_sha256": sha(ROOT / "build/adapter/CMakeFiles/videopipe_headless.dir/flags.make")},
        "compatibility": ["C++17 filesystem namespace bridge; upstream sources unchanged",
                          "preincluded standard headers for libc++"],
        "native_graph": ["RequestSource:vp_src_node", "ScenarioAdapter:vp_node", "ResponseSink:vp_node DES"],
        "transport_smoke": {"passed": smoke["ok"], "commands": smoke["commands"],
            "report": "build/transport-smoke.json", "report_sha256": sha(smoke_path),
            "tested_binary_sha256": smoke["binary_sha256"], "normal_exit_code": smoke["normal_exit_code"],
            "abnormal_worker_exit": abnormal, "rejected_shutdown": refused},
    }
    destination = ROOT / "build/cpp-build-manifest.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(destination)
    print(json.dumps({"manifest": str(destination), "binary_sha256": binaries[binary_paths[0]],
                      "smoke_commands": smoke["commands"], "abnormal_worker_exit": abnormal["adapter_exit_code"]}))


if __name__ == "__main__":
    main()
