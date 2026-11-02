#!/usr/bin/env bash
set -euo pipefail
pilot_root="$(cd "$(dirname "$0")/.." && pwd)"
expected_vp_commit=2a3b461e555725681ec2c0b3693e743e10408299
expected_ocv_sha=44c106d5bb47efec04e531fd93008b3fcd1d27138985c5baf4eafac0e1ec9e9d
actual_vp_commit="$(git -C "$pilot_root/vendor/VideoPipe" rev-parse HEAD)"
[[ "$actual_vp_commit" == "$expected_vp_commit" ]] || { echo 'Unexpected VideoPipe commit' >&2; exit 1; }
git -C "$pilot_root/vendor/VideoPipe" diff --exit-code HEAD
actual_ocv_sha="$(shasum -a 256 "$pilot_root/vendor/opencv-4.12.0.tar.gz" | cut -d ' ' -f 1)"
[[ "$actual_ocv_sha" == "$expected_ocv_sha" ]] || { echo 'Unexpected OpenCV archive hash' >&2; exit 1; }
rm -f "$pilot_root/build/cpp-build-manifest.json"
if [[ ! -f "$pilot_root/vendor/opencv-4.12.0/CMakeLists.txt" ]]; then
  tar -xzf "$pilot_root/vendor/opencv-4.12.0.tar.gz" -C "$pilot_root/vendor"
fi
cmake -S "$pilot_root/vendor/opencv-4.12.0" -B "$pilot_root/build/opencv-core" \
  -DBUILD_LIST=core -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF \
  -DBUILD_EXAMPLES=OFF -DBUILD_opencv_apps=OFF -DBUILD_JAVA=OFF \
  -DBUILD_opencv_python2=OFF -DBUILD_opencv_python3=OFF \
  -DWITH_IPP=OFF -DWITH_OPENCL=OFF -DWITH_ITT=OFF -DWITH_LAPACK=OFF \
  -DWITH_EIGEN=OFF -DWITH_ADE=OFF -DWITH_PROTOBUF=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build "$pilot_root/build/opencv-core" --target opencv_core -j 8
cmake -S "$pilot_root/cpp" -B "$pilot_root/build/adapter" -DCMAKE_BUILD_TYPE=Release
cmake --build "$pilot_root/build/adapter" -j 8
python3 "$pilot_root/cpp/smoke_transport.py"
python3 "$pilot_root/cpp/write_build_manifest.py"
