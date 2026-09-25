# TEVS-AR0822 capture for PATH Imaging

C++17 host application for the TEVS-AR0822-M-S42-IR-RPI22 camera using
TechNexion VizionSDK. This runs on the Raspberry Pi; it is not an image to flash
into the camera ISP or a replacement for the TEVS kernel driver. The full lens,
IR and adapter SKU is not verified by discovery: select the physically connected
AR0822 using the printed device name/hardware ID. The application verifies a
TechNexion MIPI device, but other TechNexion MIPI models can also pass that check.

## Prepare the Raspberry Pi

1. Install a supported 64-bit OS, the matching TechNexion TEVS kernel driver,
   and the correct camera overlay for your Pi board and CSI connector.
   Follow the vendor's [Pi 4 guide](https://developer.technexion.com/docs/technexion-tevs-camera-for-raspberry-pi-4)
   or [Pi 5 guide](https://developer.technexion.com/docs/technexion-tevs-camera-for-raspberry-pi-5).
   Do not copy kernel/overlay settings between board versions.
2. Install the ARM64 C++ [VizionSDK](https://github.com/TechNexion-Vision/vizionsdk)
   development package and runtime for that OS. Its CMake package must expose
   `vizionsdk::VizionSDK` and `VizionSDK.h`.
3. Configure the media route for the board with the vendor tools/configuration.
   Pi 5 configuration is provided in the SDK repository at
   `config/route_config/VxRoute_RaspberryPi5.yaml`; adapt it to the connected port
   following the vendor instructions. This program does not change media routes.
   Confirm discovery and capture in the vendor tool first, then close other
   camera applications. Ensure your user can access the video/media devices.
4. Install build tools: `sudo apt install build-essential cmake`.

## Build and capture

From the repository root on the Pi:

```sh
cmake -S firmware -B build/firmware -DCMAKE_BUILD_TYPE=Release
cmake --build build/firmware -j2
./build/firmware/path-camera --list
./build/firmware/path-camera --device 0 --formats
# Replace both indexes with entries printed above; choose an uncompressed mode.
./build/firmware/path-camera --device 0 --format 0 --frames 10 --output captures/run-001
```

Create the parent directory (`mkdir -p captures`) before capture. Each output
directory must be new to prevent overwriting previous captures. For an SDK
installed outside the default prefix, pass `-DCMAKE_PREFIX_PATH=/path/to/sdk`.
Resolution, frame rate and media type are taken from the selected SDK format,
including its `mediatypeIdx`; no sensor mode or exposure is hardcoded.

`frame_N.raw` contains the unmodified SDK payload. `frames.csv` records filename,
dimensions, advertised FPS, pixel format, media type and byte count for each
successfully written frame. FPS is the reported mode rate, not a measured rate;
synchronous disk writes may reduce capture throughput. Files are not PNGs and
are not sensor Bayer RAW. For example, decode a UYVY frame using the recorded
dimensions and OpenCV's `COLOR_YUV2BGR_UYVY`. No color conversion, calibration,
exposure tuning, IR illumination control or clinical interpretation is applied.

Supported capture layouts: packed UYVY/YUY2, NV12, RGB16, RGB/BGR and BGRA/BGRX.
MJPG and unknown layouts are listed but rejected for capture. `VxGetImage` has
an output size parameter, **no input capacity or stride parameter**. The program
allocates width × height × 4 bytes and requires the returned size to match the
tightly packed selected layout. This follows the vendor allocation model;
it cannot defend against an SDK writing past the supplied buffer. Validate
the SDK's packed-frame behavior on the target before relying on capture.

`--timeout-ms` accepts 1–65535 (default 2500). Timeouts and corrupted buffers
retry up to `--retries` times consecutively (default 3), resetting after a good
frame. Occupied-camera and other SDK errors fail immediately. Cleanup stops
streaming and closes the camera, including on failures. Ctrl+C/SIGTERM requests
cleanup after the current SDK call and exits 130; normal completion exits 0,
errors exit 1. Failed/interrupted runs retain already saved frames for inspection.

## Hardware-independent checks

```sh
cmake -S firmware -B build/firmware-test -DPATH_CAMERA_TESTS_ONLY=ON
cmake --build build/firmware-test -j2
ctest --test-dir build/firmware-test --output-on-failure
```

This uses an isolated fake SDK to verify CLI validation, exact bytes/metadata,
mode selection, retry bounds, SDK failures and cleanup. It does **not** establish
binary compatibility with the installed vendor SDK or validate physical capture.
On the target, build the normal target against the actual SDK, list modes,
capture several frames, inspect their decoded image and test Ctrl+C and camera
disconnect. A working camera, driver and SDK are required for that acceptance test.

API reference: [TechNexion Camera Capture](https://developer.technexion.com/docs/vision-software/vizionsdk/vizionsdk-api/camera-capture).
Build integration follows the vendor's
[C++ capture sample](https://github.com/TechNexion-Vision/vizionsdk/tree/main/samples/sample2%20-%20camera_capture).
