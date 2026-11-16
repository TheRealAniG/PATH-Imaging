# PATH-Imaging
Repository for PATH Imaging Clinic Project 2026-2027

RAW10 capture from two cameras on a Raspberry Pi 5 / CM5: the TechNexion TEVS-AR0822-M (mono, "FL", CAM/DISP 0)
and the Vision Components IMX412C (colour, CAM/DISP 1).

| | |
|---|---|
| `camera_ui.py`, `dual_stream.py` | Live window for both cameras with size, frame rate, exposure, gain, capture and record |
| `recording.py` | Lossless RAW10 video (`.j2ks`, JPEG 2000): records on the Jetson's GPU or, on the Pi, on the CPU (OpenJPH); reads recordings back; makes an `.mp4` to watch |
| `cameras/` | Capture code. The file name says which camera it is for: `imx412.py` IMX412C only (with our `Camera` class), `tevs.py` TEVS only, `shared*.py` both. See `cameras/__init__.py` |
| `scripts/capture_raw10.py` | Grab N IMX412C frames, save the last one (`.npy` + `.json`, `--png` preview) |
| `docs/vc-imx412-setup.md` | IMX412C driver and boot setup |
| `patches/`, `overlays/`, `scripts/*_camera_driver.sh` | TEVS driver build and install |

## Setup

### On the shared team Pi

The camera driver is already installed. In your worktree:

```bash
cd /opt/path/worktrees/<username>
git fetch && git merge origin/tian-dev
uv venv --system-site-packages --allow-existing
uv sync
```

No worktree yet? Run `bash /opt/path/worktrees/tian/scripts/setup_pi_user.sh <username> "Your Name" <github-email>` and add the SSH key it prints on GitHub.

If `id` doesn't list both `video` and `i2c`, run `sudo usermod -aG video,i2c $USER` and log out and back in.

### On your own Pi

```bash
sudo apt install git build-essential device-tree-compiler linux-headers-rpi-2712 v4l-utils \
  python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
curl -LsSf https://astral.sh/uv/install.sh | sh && source $HOME/.local/bin/env
git clone -b tian-dev git@github.com:TheRealAniG/PATH-Imaging.git && cd PATH-Imaging
uv venv --system-site-packages && uv sync
sudo usermod -aG video,i2c $USER
bash scripts/build_camera_driver.sh && sudo bash scripts/install_camera_driver.sh
```

Connect the camera to **CAM/DISP 0**. In `/boot/firmware/config.txt`, set `camera_auto_detect=0`, add `dtoverlay=tevs-rpi22,cam0` at the end, and reboot.

**CAM/DISP 1 on the CM5 IO Board** ties the camera's reset line (connector pin 17) high through R8, so the camera never boots (`tevs ... bootup timeout`). Fix: fit both J6 jumpers, wire GPIO 4 (header pin 7) through 330 Ω to R8's pad on the connector side (not its 3.3 V side), and use `dtoverlay=tevs-rpi22,cam1` followed by `dtoverlay=tevs-cam1-reset`. That overlay (installed by the install script) gives the driver GPIO 4 as its reset line.

Rerun the build and install scripts after every kernel update.

For the IMX412C see `docs/vc-imx412-setup.md`.

## Run

```bash
uv run python camera_ui.py                        # both cameras live; Capture saves to ~/Desktop/PATH-captures
uv run python scripts/capture_raw10.py --png      # one IMX412C RAW10 frame from Python (see cameras/imx412.py)
```

Quit with Ctrl+C, not Ctrl+Z: a paused run keeps the camera busy.

## Record

**Record** in `camera_ui.py` saves every frame of both cameras until it is pressed again, as lossless `.j2ks` files under
`~/Desktop/PATH-captures/<sensor>/<time>/`, and right after makes an `.mp4` of the live view beside each one, to watch.
`python recording.py file.j2ks` says what is in a recording and how many frames are missing; `recording.py`'s first
lines show how to read the frames in Python.

- **Jetson**: the GPU compresses. Both cameras at 24 fps, colour at full size, lose no frames (measured).
- **Pi**: the CPU compresses (OpenJPH, installed by `uv sync`), which is far slower. Not yet run on a Pi. On the
  Jetson's CPU, both cameras at 1920x1080 and 24 fps lose no frames, and full-size colour only saves about 8 frames a
  second, so pick a smaller colour size or a lower frame rate before recording. The status line counts frames lost.
- The `.mp4` needs GStreamer's `x264enc` and `h264parse` (`gstreamer1.0-plugins-ugly`, `-bad`). Without them the
  recording is still saved and the status line says why there is no `.mp4`.
