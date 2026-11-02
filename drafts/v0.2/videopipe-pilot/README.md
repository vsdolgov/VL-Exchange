# VideoPipe integration and input-queue pilot

VideoPipe is used here as an example of another execution environment to demonstrate the generality and portability of the VLSP approach. It is not a required dependency of VLSP. This local experiment runs the unchanged synthetic VLSP package using the reference host and a separately implemented host through a native C++ VideoPipe transport. The package invokes three different untrained CPU ONNX graphs and an IoU tracker. Common model loaders, ONNX Runtime and security code are used by both hosts. One pilot does not establish compatibility with all platforms; this is a transport integration test, not a third-party vendor implementation or a claim of industrial detector quality.

The source package, model snapshots and their SHA-256 inventory are in `fixtures/`. The script checks these before and after execution. The input queue is in the Python worker before scenario execution; upstream VideoPipe queues and blocked producers are not bounded by this queue. The test uses artificial gates to reproduce overload; the measured synthetic 4×2 frames are not a production benchmark.

## Build on macOS arm64

The following commands are run from the repository root. They require Git, curl, CMake, a C++17 compiler, CPython 3.12 and network access to retrieve public dependencies. Build output and downloaded source stay under the ignored `vendor/` and `build/` directories. Other operating systems have not been verified.

```sh
pilot=drafts/v0.2/videopipe-pilot
python3.12 -m venv "$pilot/.venv"
"$pilot/.venv/bin/python" -m pip install -r "$pilot/requirements.lock.txt"
mkdir -p "$pilot/vendor"
git clone https://github.com/sherlockchou86/VideoPipe.git "$pilot/vendor/VideoPipe"
git -C "$pilot/vendor/VideoPipe" checkout --detach 2a3b461e555725681ec2c0b3693e743e10408299
curl -L https://github.com/opencv/opencv/archive/refs/tags/4.12.0.tar.gz -o "$pilot/vendor/opencv-4.12.0.tar.gz"
bash "$pilot/cpp/build.sh"
```

The build script verifies the VideoPipe commit and OpenCV archive SHA-256, builds the required headless subset, runs transport smoke checks and records a build manifest. VideoPipe and OpenCV are fetched separately and remain subject to their respective upstream licenses.

## Run the comparison

```sh
pilot=drafts/v0.2/videopipe-pilot
PYTHONDONTWRITEBYTECODE=1 "$pilot/.venv/bin/python" "$pilot/python/prepare.py"
PYTHONDONTWRITEBYTECODE=1 "$pilot/.venv/bin/python" "$pilot/independent/run_comparison.py"
PYTHONDONTWRITEBYTECODE=1 "$pilot/.venv/bin/python" "$pilot/stream_load_test.py"
```

The independently implemented host's unit tests can be run separately:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="drafts/v0.2/scenario/src:drafts/v0.2/model/src:drafts/v0.2/security-runtime/src:model/src:$pilot" \
  "$pilot/.venv/bin/python" -m pytest -q --import-mode=importlib -p no:cacheprovider "$pilot/independent/tests"
```

Each run writes local results under the ignored `independent/reports/` directory. The fixed historical results are separately preserved in [reports](../reports/STREAM-VERIFICATION.md); throughput and numbers dropped during bursts vary by environment and scheduling. RTSP, codecs, GPU, durable delivery, crash recovery and asynchronous MLLM via VideoPipe were not established by this pilot.
