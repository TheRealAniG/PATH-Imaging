# PATH-Imaging
Repository for PATH Imaging Clinic Project 2026-2027

## Camera preview

Prototype for the TechNexion **TEVS-AR0822-M-S42-IR-RPI22** on a Raspberry Pi CM5.
Run from a terminal on the Pi desktop:

```bash
uv run camera_preview.py
```

Captures at 1920×1080, 60 fps in a resizable preview window. Add `--raw8` (or run
`camera_preview_raw8.py`) for RAW8. Close with **Q / Esc**, the window close button,
or **Ctrl+C**. Edit `WIDTH, HEIGHT, FPS` in `camera_preview.py` to change mode.
This prototype does not save images.

### Headless capture

```bash
uv run camera_capture.py --frames 30
```

Saves monochrome PGM images in a new timestamped `captures/` directory, then
stops and closes the camera. No desktop or GTK is needed. Defaults to
1920×1080 at a requested 60 fps. Choose a supported mode with `--width`,
`--height`, and `--fps`; add `--raw8` for sensor RAW8 or `--output-dir` for a
new output directory. Disk writes may reduce the rate of saved frames.

Captured PGM images under `captures/` are tracked through Git LFS. Install
`git-lfs` and run `git lfs install` before committing captures. Git stores
pointers; LFS stores the image data. After cloning, `git lfs pull` downloads the
images. The sample run in `captures/uyvy-20260930-183613-255940/` contains ten
verified, distinct 1920×1080 frames captured with a requested 60 fps mode;
the test does not establish sustained capture at 60 fps.

### Capture API (`camera_API.py`)

`camera_API.py` is TechNexion's pyvizionsdk plus RAW8. Every `Vx...` function and
type is the SDK's own, except that `VxSetFormat`, `VxStartStreaming`, `VxGetImage`
and `VxStopStreaming` capture RAW8 modes through `v4l2-ctl`. The SDK already lists
RAW8 modes but as `VX_IMAGE_FORMAT_NONE` (its enum has no RAW8), and it can only
stream UYVY; `camera_API.VX_IMAGE_FORMAT_RAW8` names those entries.

```python
import camera_API as vx
result, camera_list = vx.VxDiscoverCameraDevices()
vxcam = vx.VxInitialCameraDevice(0)
vx.VxOpen(vxcam)
result, format_list = vx.VxGetFormatList(vxcam)
fmt = next(f for f in format_list if f.format == vx.VX_IMAGE_FORMAT_RAW8
           and (f.width, f.height, f.framerate) == (1280, 720, 30))
vx.VxSetFormat(vxcam, fmt)
vx.VxStartStreaming(vxcam)
result, image = vx.VxGetImage(vxcam, 2500, fmt)   # flat uint8, reshape to (720, 1280)
vx.VxStopStreaming(vxcam)
vx.VxClose(vxcam)
```

Python dependencies are declared in `pyproject.toml` and locked in `uv.lock`.
Run `uv sync` once, or let `uv run` install them automatically. The SDK comes from
TechNexion's configured package index; uv uses the shared `/opt/path/uv-cache`.
No environment activation or manual pip installation is needed. uv manages its
ignored `.venv` automatically. Camera controls require membership of the `i2c`
group. `camera_gui.py` makes Debian's GTK/GStreamer bindings available to the
uv-managed system Python 3.13 interpreter.

**CM5 workaround (`vizion_cm5.py`).** The SDK only accepts the Pi 5 board ID and finds
no camera on a CM5 (`raspberrypi,5-compute-module`), although the camera hardware path
is identical. `vizion_cm5` (imported by `camera_API`) restarts Python with
`scripts/cm5_board_id_shim.c` preloaded, which shows the SDK the Pi 5 board ID, and
enables the CSI-2 capture link that is off after every boot. Run other SDK scripts
unchanged with `python vizion_cm5.py script.py`. Because it restarts Python, it cannot
be used from `python -` or an interactive session.

### RAW8 preview

```bash
uv run camera_preview_raw8.py
```

Shows unprocessed 8-bit sensor data (the camera's ISP is bypassed): darker, noisier,
with dark corners because lens-shading correction is skipped. It needs the patched
driver: `build_camera_driver.sh` applies `patches/tevs-raw8.patch`, which adds the
`Y8_1X8` format and requests AP1302-style `PREVIEW_FORMAT` 0x80 (RAW8, sensor) for
it. UYVY remains the default. Confirmed on
AR0822 firmware 25.10.0.1 with kernel 6.18; ISP mono (0x52, `bypass_isp=0`) returns
black frames. `scripts/test_raw8.sh` repeats the hardware comparison.

## First-time setup on this CM5

Inspection on 2026-09-22 found Debian 13 / ARM64, kernel
`6.12.62+rpt-rpi-2712`, no TEVS module, and no sensor entities on either CSI
controller. `/boot/firmware/config.txt` includes
`config_vc-mipi-driver-bcm2712.txt`, which enables Vision Components Sony camera
overlays on both ports. Those overlays conflict with this camera's setup.

The steps below need a local terminal with working `sudo`. The development
session could not authenticate sudo. No boot files or system packages were changed.

### 1. Install preview dependencies

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 \
  gstreamer1.0-gtk3 gstreamer1.0-plugins-base gstreamer1.0-plugins-good v4l-utils
```

Use `uv sync` to install the Python dependencies, then `uv run camera_preview.py`
to launch. The project selects system Python 3.13 and loads the OS-provided
GTK/GStreamer bindings through `camera_gui.py`. The vendor's full VizionSDK
installation is not required for this preview.

### 2. Build and install the TEVS driver

The build helper selects pinned TechNexion source for the running kernel:
`6611bfe782c3606ef960cae74c1220ad09f4dbce` for 6.12 or
`c681dd09c0e815d2ace2065eff867f02702c32a8` for 6.18.
Build tools and matching headers are already present on this Pi.
On another installation, install `build-essential`, `git`, `device-tree-compiler`,
and headers matching `uname -r` first.

```bash
bash scripts/build_camera_driver.sh
```

Outputs are in `.camera-driver/` (git-ignored). This command only builds files;
it does not install anything. The module successfully compiled on this CM5.
Install it for the **same kernel** it was built against:

```bash
sudo bash scripts/install_camera_driver.sh
sudo usermod -aG video "$USER"
```

Rebuild/reinstall the module after changing the kernel. Installation checks the
kernel version and loads the driver without rebooting; initial boot-overlay
configuration still requires a reboot. This helper targets the
6.12 and 6.18 ARM64 kernel families; do not install a vendor prebuilt module for a different
kernel release.

### 3. Configure the camera port and reboot

**CM5 IO Board hardware prerequisite:** CAM/DISP 1 requires **both J6 jumpers**
fitted as indicated by the board's silkscreen. These route the camera's I²C
signals; a fully seated ribbon alone is not sufficient. Shut down and disconnect
power before fitting jumpers. See the [official CM5 camera instructions](https://www.raspberrypi.com/documentation/computers/compute-module.html#attach-a-camera-module).
Missing jumpers are one possible cause of `pca953x ... error -121` followed by
`tevs ... supplier ...0027 not ready`; that error alone does not establish the cause.

Back up and edit the boot configuration:

```bash
sudo cp -a /boot/firmware/config.txt "/boot/firmware/config.txt.before-tevs-$(date +%Y%m%d-%H%M%S)"
sudo nano /boot/firmware/config.txt
```

Change `camera_auto_detect=1` to `camera_auto_detect=0`. Comment out the existing
`include config_vc-mipi-driver-bcm2712.txt` line. Under the final `[all]` section,
add the overlay for the connector actually used:

```ini
# CAM/DISP 1:
dtoverlay=tevs-rpi22
# For CAM/DISP 0 instead, use: dtoverlay=tevs-rpi22,cam0
```

Power down before reseating a ribbon cable. After saving the configuration,
reboot when ready:

```bash
sudo reboot
```

### 4. Verify and preview

```bash
uv run camera_preview.py
```

If discovery still fails, inspect `sudo dmesg | grep -iE 'tevs|csi|i2c'` and
`media-ctl -d /dev/media0 -p` (also check `/dev/media1`). A working sensor appears
as a `tevs ...` entity. Codec/ISP video nodes alone do not indicate a camera.
For permission errors, verify `id` includes `video` after reboot.
If frames fail, close other camera applications and inspect the kernel log.

To undo the boot changes, restore the timestamped `config.txt` backup and reboot.

## Validation and references

The user confirmed live capture on this CM5. The simplified preview was also
checked with a synthetic GStreamer source; that check does not verify the sensor.

- [TechNexion Raspberry Pi 5 setup and media pipeline](https://developer.technexion.com/docs/embedded-vision/mipi-csi-2/raspberrypi/raspberry-pi-5)
- [TechNexion kernel driver source](https://github.com/TechNexion-Vision/tn-rpi-camera-driver/tree/tn_rpi_kernel-6.12)
- [TechNexion Python SDK](https://github.com/TechNexion-Vision/vizionsdk-python)
- [GStreamer GTK sink](https://gstreamer.freedesktop.org/documentation/gtk/gtksink.html)

## Recovery after the September 23 kernel upgrade

The upgrade from 6.12.62 to 6.18.50 left TEVS installed only for the old
kernel. A missing TEVS module makes the preview report zero cameras. Recover
with:

```bash
bash scripts/build_camera_driver.sh
sudo bash scripts/install_camera_driver.sh
uv run camera_preview.py
```

The driver is installed manually, so repeat the build/install after kernel
updates. Unsupported kernel families require a compatible vendor driver first.
