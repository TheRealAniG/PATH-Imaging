# PATH-Imaging
Repository for PATH Imaging Clinic Project 2026-2027

## Camera: TEVS-AR0822-M on a Raspberry Pi CM5

Capture from the TechNexion **TEVS-AR0822-M-S42-IR-RPI22** (8 MP mono) on a Raspberry
Pi CM5, in the camera's normal UYVY output or in **RAW8**: unprocessed sensor data with
the camera's ISP bypassed.

| File | Purpose |
|---|---|
| `camera_API.py` | TechNexion's pyvizionsdk plus RAW8. Run it for a 10 s live RAW8 view. |
| `vizion_cm5.py`, `scripts/cm5_board_id_shim.c` | Workaround so pyvizionsdk finds the camera on a CM5. |
| `patches/tevs-raw8.patch` | TEVS driver change that makes RAW8 possible. |
| `scripts/build_camera_driver.sh`, `scripts/install_camera_driver.sh` | Build and install the patched driver. |

### Run

From the repo folder, on the Pi desktop:

```bash
uv run python camera_API.py   # 10 s live RAW8, saves the last frame as raw8.pgm
```

Use Ctrl+C, not Ctrl+Z: a paused run keeps the camera busy.

### `camera_API.py`

Use it exactly like pyvizionsdk. Every `Vx...` function and type is the SDK's own,
except that `VxSetFormat`, `VxStartStreaming`, `VxGetImage` and `VxStopStreaming` stream
RAW8 through `v4l2-ctl`. The SDK already lists the RAW8 modes, but as
`VX_IMAGE_FORMAT_NONE` (its enum has no RAW8), and can only stream UYVY.
`vx.VX_IMAGE_FORMAT_RAW8` names those entries.

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

Scripts must be run from a file (not `python -` or the `>>>` prompt), because
`vizion_cm5` restarts Python. For brevity the RAW8 path assumes the sensor is
`tevs 10-0048` on `/dev/media0` and `/dev/video0`, as on this CM5, and ignores
`VxGetImage`'s timeout.

**How RAW8 works.** The camera module's format register (0x3104) uses the AP1302 ISP
encoding: the stock driver always writes 0x50 (UYVY); 0x80 is RAW8 straight from the
sensor. `patches/tevs-raw8.patch` adds a `Y8_1X8` format that writes 0x80 and labels the
data RAW8 for the Pi's CSI-2 receiver. UYVY remains the default, so other software is
unaffected. Confirmed on AR0822 firmware 25.10.0.1 with kernel 6.18; ISP mono (0x52)
returns black frames.

**CM5 workaround.** VizionSDK only accepts the Pi 5 board ID and finds no camera on a
CM5 (`raspberrypi,5-compute-module`), although the camera path is identical.
`vizion_cm5` (imported by `camera_API`) restarts Python with
`scripts/cm5_board_id_shim.c` preloaded, which shows the SDK the Pi 5 ID for that
process only, and enables the CSI-2 capture link, which is off after every boot. Run
other SDK scripts unchanged with `python vizion_cm5.py script.py`. Remove once
TechNexion supports the CM5.

## Setup

### 1. Python environment

`uv sync` installs `pyvizionsdk` (from TechNexion's index, declared in `pyproject.toml`).
The live window also needs the system GTK/GStreamer bindings, which a uv venv only
sees if it is created with system packages, once per worktree:

```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good v4l-utils
uv venv --system-site-packages --allow-existing
uv sync
sudo usermod -aG video,i2c "$USER"   # then log out and back in
```

The `i2c` group is needed for the SDK's camera controls (exposure, gain, …).

### 2. Build and install the TEVS driver

The build script checks out pinned TechNexion source for the running kernel
(`6611bfe7…` for 6.12, `c681dd09…` for 6.18), applies `patches/tevs-raw8.patch`, and
builds into `.camera-driver/` (git-ignored). The patch was written and tested on 6.18.
Install `build-essential`, `git`, `device-tree-compiler` and headers matching
`uname -r` first if missing.

```bash
bash scripts/build_camera_driver.sh
sudo bash scripts/install_camera_driver.sh
```

The install script reloads the driver in place. Repeat both after every kernel update:
the driver is built for one kernel version. Check that the patched driver is loaded
with `modinfo tevs | grep bypass_isp`.

### 3. Configure the camera port (first time only)

**CM5 IO Board:** CAM/DISP 1 requires **both J6 jumpers** fitted as shown on the
silkscreen; they route the camera's I²C signals. Power down before fitting jumpers or
reseating the ribbon. See the
[official CM5 camera instructions](https://www.raspberrypi.com/documentation/computers/compute-module.html#attach-a-camera-module).
Missing jumpers can cause `pca953x ... error -121` followed by
`tevs ... supplier ...0027 not ready`.

Back up and edit the boot configuration:

```bash
sudo cp -a /boot/firmware/config.txt "/boot/firmware/config.txt.before-tevs-$(date +%Y%m%d-%H%M%S)"
sudo nano /boot/firmware/config.txt
```

Change `camera_auto_detect=1` to `camera_auto_detect=0`. Comment out
`include config_vc-mipi-driver-bcm2712.txt` (its Vision Components overlays conflict).
Under the final `[all]` section add:

```ini
# CAM/DISP 1:
dtoverlay=tevs-rpi22
# For CAM/DISP 0 instead, use: dtoverlay=tevs-rpi22,cam0
```

Then `sudo reboot`. To undo, restore the timestamped backup and reboot.

### Troubleshooting

- **No camera found:** `sudo dmesg | grep -iE 'tevs|csi|i2c'` should end with
  `tevs 10-0048: probe success`; `media-ctl -d /dev/media0 -p` should list a
  `tevs 10-0048` entity. After a kernel update, rebuild and reinstall the driver.
- **No or incomplete frames:** another program holds the camera, often a run paused
  with Ctrl+Z. Check `jobs` and end it.

## References

- [TechNexion Raspberry Pi 5 setup and media pipeline](https://developer.technexion.com/docs/embedded-vision/mipi-csi-2/raspberrypi/raspberry-pi-5)
- [TechNexion kernel driver source](https://github.com/TechNexion-Vision/tn-rpi-camera-driver/tree/tn_rpi_kernel-6.18)
- [VizionSDK capture API](https://developer.technexion.com/docs/vision-software/vizionsdk/vizionsdk-api/camera-capture)
- [pyvizionsdk](https://github.com/TechNexion-Vision/vizionsdk-python)
