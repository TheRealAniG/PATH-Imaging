# PATH-Imaging
Repository for PATH Imaging Clinic Project 2026-2027

## Camera: TEVS-AR0822-M on a Raspberry Pi CM5

Capture from the TechNexion **TEVS-AR0822-M-S42-IR-RPI22** (8 MP mono) on a Raspberry
Pi CM5, in the camera's normal UYVY output or in **RAW10**: unprocessed 10-bit sensor data with
the camera's ISP bypassed.

| File | Purpose |
|---|---|
| `camera_API.py` | TechNexion's pyvizionsdk plus RAW10. Run it for a 10 s live RAW10 view. |
| `vizion_cm5.py`, `scripts/cm5_board_id_shim.c` | Workaround so pyvizionsdk finds the camera on a CM5. |
| `patches/tevs-raw.patch` | TEVS driver change that adds RAW8 and RAW10. |
| `scripts/build_camera_driver.sh`, `scripts/install_camera_driver.sh` | Build and install the patched driver. |
| `scripts/setup_pi_user.sh` | New user on the shared Pi: steps 3–11 of the team setup guide. |

### Run

After the setup below, from the repo folder in a terminal on the Pi desktop:

```bash
uv run python camera_API.py   # 10 s live RAW10, saves the last frame as raw10.pgm (16-bit, max 1023)
```

Use Ctrl+C, not Ctrl+Z: a paused run keeps the camera busy.

### `camera_API.py`

Use it exactly like pyvizionsdk. Every `Vx...` function and type is the SDK's own,
except that `VxSetFormat`, `VxStartStreaming`, `VxGetImage` and `VxStopStreaming` stream
RAW10 through `v4l2-ctl`. The SDK already lists the RAW modes, but as
`VX_IMAGE_FORMAT_NONE` (its enum has no RAW formats), and can only stream UYVY.
`vx.VX_IMAGE_FORMAT_RAW10` names those entries. RAW10 frames come back as flat `uint16`
arrays of 0–1023 (the Pi unpacks each 10-bit pixel into 16 bits, `Y16`).

```python
import camera_API as vx
result, camera_list = vx.VxDiscoverCameraDevices()
vxcam = vx.VxInitialCameraDevice(0)
vx.VxOpen(vxcam)
result, format_list = vx.VxGetFormatList(vxcam)
fmt = next(f for f in format_list if f.format == vx.VX_IMAGE_FORMAT_RAW10
           and (f.width, f.height, f.framerate) == (1280, 720, 30))
vx.VxSetFormat(vxcam, fmt)
vx.VxStartStreaming(vxcam)
result, image = vx.VxGetImage(vxcam, 2500, fmt)   # flat uint16 0-1023, reshape to (720, 1280)
vx.VxStopStreaming(vxcam)
vx.VxClose(vxcam)
```

Scripts must be run from a file (not `python -` or the `>>>` prompt), because
`vizion_cm5` restarts Python. For brevity the RAW10 path assumes the sensor is
`tevs 10-0048` on `/dev/media0` and `/dev/video0`, as on this CM5, and ignores
`VxGetImage`'s timeout.

**How RAW works.** The camera module's format register (0x3104) uses the AP1302 ISP
encoding: format type in the upper 4 bits, so the stock driver's 0x50 is UYVY, 0x80 is
RAW8 and 0x90 is RAW10, straight from the sensor. `patches/tevs-raw.patch` adds
`Y8_1X8` (writes 0x80) and `Y10_1X10` (writes 0x90) formats and labels the data RAW8/RAW10
for the Pi's CSI-2 receiver. UYVY remains the default, so other software is unaffected.
Both confirmed on AR0822 firmware 25.10.0.1 with kernel 6.18: RAW10 frames use all 10 bits
and match RAW8 ×4 (correlation 0.993). ISP mono (0x52) returns black frames. Without
`camera_API.py`, capture RAW10 with `media-ctl` (`Y10_1X10` on the sensor and both `csi2`
pads, `field:none`) and `v4l2-ctl` (`pixelformat='Y16 '`, or `Y10P` for the packed form).

**CM5 workaround.** VizionSDK only accepts the Pi 5 board ID and finds no camera on a
CM5 (`raspberrypi,5-compute-module`), although the camera path is identical.
`vizion_cm5` (imported by `camera_API`) restarts Python with
`scripts/cm5_board_id_shim.c` preloaded, which shows the SDK the Pi 5 ID for that
process only, and enables the CSI-2 capture link, which is off after every boot. Run
other SDK scripts unchanged with `python vizion_cm5.py script.py`. Remove once
TechNexion supports the CM5.

## Quick start A: the shared team Pi

The patched driver and boot config are already installed on the shared Pi, so you only
need the code and a Python environment. If you have no worktree yet, first follow the
team "Raspberry Pi Setup" guide, or let the script do steps 3–11 for you:

```bash
cd /opt/path/worktrees/tian    # the script lives here until tian-dev is merged into main
bash scripts/setup_pi_user.sh <username> "Your Name" <github-email>
```

It prints an SSH key to add on GitHub (Settings → SSH and GPG keys); test with
`ssh -T git@github.com`. Then bring this branch into your own branch and run:

```bash
cd /opt/path/worktrees/<username>
git fetch
git merge origin/tian-dev                          # adds the camera code to your branch
uv venv --system-site-packages --allow-existing    # once: lets uv see the system GTK/GStreamer
uv sync                                            # installs pyvizionsdk
uv run python camera_API.py
```

You can't `git switch tian-dev` in your worktree while it is checked out in Tian's;
merging (or `git switch -c <username>-camera origin/tian-dev`) avoids that. You also need
the `video` and `i2c` groups: if `id` doesn't list both, run
`sudo usermod -aG video,i2c $USER` and log out and back in. The camera can only be used
by one program at a time, so check nobody else is streaming.

## Quick start B: your own Raspberry Pi 5 / CM5

For a fresh **Raspberry Pi OS 64-bit (Debian 13)** with kernel **6.18** (`uname -r`;
6.12 is also supported by the build script, but the patch was only tested on 6.18).
Steps 4 and 5 need `sudo`; step 5 needs a reboot.

**1. Packages** (kernel headers must match `uname -r`; if apt installs newer headers than
the running kernel, run `sudo apt full-upgrade`, reboot, and repeat):

```bash
sudo apt update
sudo apt install git build-essential device-tree-compiler linux-headers-rpi-2712 v4l-utils \
  python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good
curl -LsSf https://astral.sh/uv/install.sh | sh    # then open a new terminal
```

**2. Code and Python environment:**

```bash
git clone -b tian-dev git@github.com:TheRealAniG/PATH-Imaging.git   # needs an SSH key on GitHub
cd PATH-Imaging
uv venv --system-site-packages
uv sync
```

Without an SSH key on that Pi, clone `https://github.com/TheRealAniG/PATH-Imaging.git`
instead and use a GitHub personal access token as the password.

**3. Permissions:** `sudo usermod -aG video,i2c $USER`, then log out and back in. `video`
gives camera access; `i2c` is needed by the SDK's camera controls (exposure, gain, …).

**4. Build and install the patched driver:**

```bash
bash scripts/build_camera_driver.sh          # downloads TechNexion's source, applies patches/tevs-raw.patch
sudo bash scripts/install_camera_driver.sh   # installs tevs.ko + the tevs-rpi22 overlay, loads the driver
```

The driver is built for one kernel version: repeat both commands after every kernel
update, or the camera disappears. Check with `modinfo tevs | grep bypass_isp`.

**5. Boot configuration (first time only).** Power down and connect the camera ribbon. On a
**CM5 IO Board**, the connector you use needs **both of its J6 jumpers** fitted as shown on
the silkscreen (they route the camera's I²C); see the
[CM5 camera instructions](https://www.raspberrypi.com/documentation/computers/compute-module.html#attach-a-camera-module).
Missing jumpers can cause `pca953x ... error -121` followed by
`tevs ... supplier ...0027 not ready`. Then back up and edit the boot config:

```bash
sudo cp -a /boot/firmware/config.txt "/boot/firmware/config.txt.before-tevs-$(date +%Y%m%d-%H%M%S)"
sudo nano /boot/firmware/config.txt
```

Set `camera_auto_detect=0`. If there is an `include config_vc-mipi-driver-bcm2712.txt`
line, comment it out (its Vision Components overlays conflict). Under the final `[all]`
section, add **one** line for the connector the camera is on:

```ini
dtoverlay=tevs-rpi22,cam0    # CAM/DISP 0 (what the shared team Pi uses)
# dtoverlay=tevs-rpi22       # CAM/DISP 1 instead
```

Save, then `sudo reboot`. To undo, restore the timestamped backup and reboot.

**6. Check and run:**

```bash
dmesg | grep tevs                 # should include: tevs 10-0048: probe success
uv run python camera_API.py       # 10 s live RAW10 window, saves raw10.pgm
```

On a regular Pi 5 (not a CM5), `vizion_cm5` does nothing and the J6 jumpers don't apply.

## Troubleshooting

- **No camera found** (`camera_API.py` fails with `StopIteration` or an empty camera list): `dmesg | grep -iE 'tevs|csi|i2c'` should include
  `tevs 10-0048: probe success`, and `media-ctl -d /dev/media0 -p` should list a
  `tevs 10-0048` entity. After a kernel update, rebuild and reinstall the driver (step 4).
- **`No module named gi` / no window:** the uv venv can't see system packages; run
  `uv venv --system-site-packages --allow-existing` then `uv sync`.
- **`Permission denied` on `/dev/video0` or `/dev/i2c-10`:** add the `video` and `i2c`
  groups (step 3) and log out and back in.
- **`uv sync` fails with `Permission denied` in `/opt/path/uv-cache`** (shared Pi only):
  someone ran `uv` with `sudo`; fix with `sudo chmod -R g+w /opt/path/uv-cache`.
- **No or incomplete frames:** another program holds the camera, often a run paused with
  Ctrl+Z. Check `jobs` and end it; quit with Ctrl+C instead.
- **RAW10 fails but UYVY works:** the stock driver is loaded, not the patched one;
  `modinfo tevs | grep bypass_isp` prints nothing. Redo step 4.
- **Camera on a different `/dev/media*` or `/dev/video*`:** `camera_API.py` assumes
  `/dev/media0` and `/dev/video0` (true when the camera is the only one). Find yours with
  `for m in /dev/media*; do media-ctl -d $m -p | grep -q tevs && echo $m && media-ctl -d $m -e rp1-cfe-csi2_ch0; done`
  and edit the two paths in `camera_API.py`.

## References

- [TechNexion Raspberry Pi 5 setup and media pipeline](https://developer.technexion.com/docs/embedded-vision/mipi-csi-2/raspberrypi/raspberry-pi-5)
- [TechNexion kernel driver source](https://github.com/TechNexion-Vision/tn-rpi-camera-driver/tree/tn_rpi_kernel-6.18)
- [VizionSDK capture API](https://developer.technexion.com/docs/vision-software/vizionsdk/vizionsdk-api/camera-capture)
- [pyvizionsdk](https://github.com/TechNexion-Vision/vizionsdk-python)
