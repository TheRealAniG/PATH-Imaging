# TEVS-AR0822 live preview

Minimal C++ preview for the TEVS-AR0822-M-S42-IR-RPI22:
discover camera 0, open it, choose its first UYVY format (MJPG fallback),
start streaming, retrieve frames and display them with OpenCV.
Press **q**, **Esc**, or close the preview window to stop and close the camera.

On the Raspberry Pi, install the matching TechNexion TEVS driver, configure the
camera media route, and install the ARM64 VizionSDK development package first.
Run in a desktop session with OpenCV GUI support.

From the repository root:

```sh
sudo apt install build-essential cmake libopencv-dev
cmake -S firmware -B build/preview
cmake --build build/preview -j2
./build/preview/path-camera
```

The SDK must expose the CMake target `vizionsdk::VizionSDK`. If installed in a
custom location, add `-DCMAKE_PREFIX_PATH=/path/to/sdk` when configuring.
The buffer follows the vendor example's allocation model; `VxGetImage` has no
capacity argument. UYVY is expected to be tightly packed. MJPG uses a
width ¡Á height ¡Á 4 buffer; confirm this is sufficient with your SDK/mode.

Reference: [Camera Capture API](https://developer.technexion.com/docs/vision-software/vizionsdk/vizionsdk-api/camera-capture).
Real SDK compilation and live preview need validation on the camera-equipped Pi.
