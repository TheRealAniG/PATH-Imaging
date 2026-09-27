# TEVS-AR0822 preview

Opens camera 0, selects its first UYVY format, and displays the mono (Y)
channel with OpenCV.
Press **q** or **Esc** to quit. Capture errors stop the program.

## 1. Prepare the Raspberry Pi

Use a Raspberry Pi 4 or 5 running a 64-bit Linux system. Install the matching
TechNexion TEVS camera driver and configure the camera media route first.
Run the preview from a desktop session so OpenCV can display its window.

## 2. Install VizionSDK

These ARM64 commands follow TechNexion's
[C++ installation guide](https://developer.technexion.com/docs/vision-software/vizionsdk/cplusplus/vizionsdk-cpp-installation).
Run them on the Pi.

Install the tools and add TechNexion's signing key:

```sh
sudo apt update
sudo apt install wget gpg
wget -qO- https://download.technexion.com/apt/technexion.asc | gpg --dearmor > packages.technexion.gpg
sudo install -D -o root -g root -m 644 packages.technexion.gpg /etc/apt/keyrings/packages.technexion.gpg
```

Add the ARM64 package repositories:

```sh
sudo sh -c 'echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/packages.technexion.gpg] https://download.technexion.com/apt/vizionsdk/ stable main" > /etc/apt/sources.list.d/vizionsdk.list'
sudo sh -c 'echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/packages.technexion.gpg] https://download.technexion.com/apt/vizionviewer/ stable main" >> /etc/apt/sources.list.d/vizionsdk.list'
sudo apt update
sudo apt install vizionsdk
```

## 3. Build and run

Install this project's build and preview dependencies:

```sh
sudo apt install build-essential cmake libopencv-dev
```

From the PATH-Imaging repository root:

```sh
cmake -S firmware -B build/preview
cmake --build build/preview -j2
./build/preview/path-camera
```

## 4. Python setup and preview

`src/pyvizionSample.py` runs the same camera-0 UYVY preview using the Python
API. Complete the camera/SDK setup above first. No C++ build is needed for this
sample. The commands below follow the vendor's
[Python installation guide](https://developer.technexion.com/docs/vision-software/vizionsdk/python/pyvizionsdk-installation),
using a separate environment and the vendor package index.

From the repository root on the Pi:

```sh
sudo apt install python3-pip python3-venv
python3 -m venv firmware/.venv
source firmware/.venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pyvizionsdk --extra-index-url https://pypi.vizionsdk.com/root/pyvizionsdk/+simple/
python -m pip install numpy opencv-python
python firmware/src/pyvizionSample.py
```

Use a supported CPython version (the vendor lists 3.8-3.14) with an ARM64 wheel
matching your interpreter. Use `opencv-python`, not its headless variant, for
the preview window. Run in a desktop session. Press **q** or **Esc** to quit;
Ctrl+C also closes the camera. Run `deactivate` when finished.

Python's `VxGetImage(camera, timeout, format)` returns `(result, frame)`;
the sample reshapes the returned UYVY buffer and displays it with OpenCV.
Python syntax can be checked locally; SDK imports and physical capture still
need testing on the Pi.

## I2C permission errors

If the SDK reports I2C permission errors, the vendor recommends granting access
through the `i2c` group:

```sh
sudo groupadd -f i2c
sudo usermod -aG i2c "$USER"
sudo nano /etc/udev/rules.d/99-i2c.rules
```

Add this rule, save the file, then reboot:

```text
KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"
```

```sh
sudo reboot
```

## Capture flow

Uses the [C++ Camera Capture API](https://developer.technexion.com/docs/vision-software/vizionsdk/vizionsdk-api/camera-capture) directly:

`VxDiscoverCameraDevices` -> `VxInitialCameraDevice` -> `VxOpen` ->
`VxGetFormatList` -> `VxSetFormat` -> `VxStartStreaming` ->
`VxGetImage` -> `VxStopStreaming` -> `VxClose`.

Requires tightly packed UYVY (width * height * 2 bytes). The SDK's `VxGetImage`
has no buffer-capacity argument; confirm this layout on the target SDK.
Real SDK compilation and camera preview have not been validated locally.

## Why there is no RAW format (and the Y8 workaround)

TechNexion's Raspberry Pi driver (`tn_rpi_kernel-6.12`) lists only
`UYVY8_1X16` for the AR0822 (`tevs_tbls.h`). It also writes UYVY to the camera's
on-board ISP every time it configures a stream (`tevs_main.c`). VizionSDK only
reports formats the driver exposes, and its `VX_IMAGE_FORMAT` has no GREY or
RAW entry.

This camera is mono, so UYVY already carries the full 8-bit image in Y
(U/V are constant). The samples above convert UYVY to grayscale, so no
driver change is needed for mono pixels.

To stream 8-bit mono directly (half the bandwidth of UYVY), apply
`driver/tevs-ar0822-y8.patch`. It adds `Y8_1X8` to the AR0822 and programs the
ISP for Y8 over CSI-2 RAW8. The Pi 5 (rp1-cfe) and Pi 4 (unicam) receivers
already accept Y8 as `GREY`. Run these on the Pi after TechNexion's normal
driver install:

```sh
sh firmware/driver/install_tevs_y8.sh
sudo reboot
sh firmware/driver/setup_y8.sh 1920 1080      # prints the video node
python firmware/src/monoPreview.py /dev/video0 1920 1080
```

Replace `/dev/video0` with the node `setup_y8.sh` prints. Capture one raw frame
without OpenCV:

```sh
v4l2-ctl -d /dev/video0 --stream-mmap --stream-count=1 --stream-to=frame.gray
```

UYVY stays the default format, so VizionSDK and the samples above keep working.

**Experimental ISP bypass:** the module parameter `bypass_isp=1` makes Y8 request
TEVS format `0x80` (RAW8, ISP bypassed) instead of `0x52`. TechNexion does not
document that value for this module, so frames may not arrive. Try it with
`sudo modprobe -r tevs && sudo modprobe tevs bypass_isp=1`. Reload without the
parameter to undo. Output above 8 bits is not exposed by any TEVS driver.
