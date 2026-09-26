# TEVS-AR0822 preview

Uses the [VizionSDK C++ Camera Capture API](https://developer.technexion.com/docs/vision-software/vizionsdk/vizionsdk-api/camera-capture) directly:

`VxDiscoverCameraDevices` -> `VxInitialCameraDevice` -> `VxOpen` ->
`VxGetFormatList` -> `VxSetFormat` -> `VxStartStreaming` ->
`VxGetImage` -> `VxStopStreaming` -> `VxClose`.

Opens camera 0, selects its first UYVY format, and displays frames with OpenCV.
Press **q** or **Esc** to quit. Capture errors stop the program.

On the Pi, install the matching TEVS driver and VizionSDK development package,
configure the camera media route, and run from a desktop session:

```sh
sudo apt install build-essential cmake libopencv-dev
cmake -S firmware -B build/preview
cmake --build build/preview -j2
./build/preview/path-camera
```

Requires tightly packed UYVY (width * height * 2 bytes). The SDK's `VxGetImage`
has no buffer-capacity argument; confirm this layout on the target SDK.
Real SDK compilation and camera preview have not been validated locally.
