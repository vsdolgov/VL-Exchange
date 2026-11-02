cmake_minimum_required(VERSION 3.19)
project(vlsp_videopipe_integration LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(VP_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../vendor/VideoPipe")
set(OCV_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../vendor/opencv-4.12.0")
set(OCV_BUILD "${CMAKE_CURRENT_SOURCE_DIR}/../build/opencv-core")
find_package(Threads REQUIRED)
file(GLOB VP_OBJECTS "${VP_ROOT}/objects/*.cpp" "${VP_ROOT}/objects/ba/*.cpp" "${VP_ROOT}/objects/shapes/*.cpp")
add_library(videopipe_headless STATIC
  "${VP_ROOT}/nodes/vp_node.cpp"
  "${VP_ROOT}/nodes/vp_src_node.cpp"
  "${VP_ROOT}/nodes/vp_meta_publisher.cpp"
  "${VP_ROOT}/nodes/vp_meta_subscriber.cpp"
  "${VP_ROOT}/utils/logger/vp_logger.cpp"
  "${VP_ROOT}/utils/logger/vp_log_file_writer.cpp"
  ${VP_OBJECTS})
target_include_directories(videopipe_headless PUBLIC
  "${CMAKE_CURRENT_SOURCE_DIR}/compat"
  "${VP_ROOT}" "${VP_ROOT}/third_party/nlohmann"
  "${OCV_BUILD}" "${OCV_ROOT}/include"
  "${OCV_ROOT}/modules/core/include" "${OCV_ROOT}/modules/imgproc/include"
  "${OCV_ROOT}/modules/highgui/include" "${OCV_ROOT}/modules/videoio/include"
  "${OCV_ROOT}/modules/imgcodecs/include")
# Upstream headers omit several transitive standard includes that GCC provides.
target_compile_options(videopipe_headless PUBLIC -include "${CMAKE_CURRENT_SOURCE_DIR}/compat/standard_includes.h")
target_link_libraries(videopipe_headless PUBLIC
  "${OCV_BUILD}/lib/libopencv_core.a"
  "${OCV_BUILD}/3rdparty/lib/libzlib.a"
  "${OCV_BUILD}/3rdparty/lib/libtegra_hal.a"
  Threads::Threads)
if(APPLE)
  target_link_libraries(videopipe_headless PUBLIC "-framework Accelerate" "-framework CoreFoundation")
endif()
execute_process(COMMAND git -C "${VP_ROOT}" rev-parse HEAD OUTPUT_VARIABLE VP_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
add_executable(vlsp_videopipe adapter.cpp)
target_link_libraries(vlsp_videopipe PRIVATE videopipe_headless)
target_compile_definitions(vlsp_videopipe PRIVATE VP_UPSTREAM_COMMIT="${VP_COMMIT}")
