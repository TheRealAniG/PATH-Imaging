# PATH-Imaging
Repository for PATH Imaging Clinic Project 2026-2027

RAW10 capture from the TechNexion TEVS-AR0822-M camera on a Raspberry Pi 5 / CM5.

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
  python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good
curl -LsSf https://astral.sh/uv/install.sh | sh && source $HOME/.local/bin/env
git clone -b tian-dev git@github.com:TheRealAniG/PATH-Imaging.git && cd PATH-Imaging
uv venv --system-site-packages && uv sync
sudo usermod -aG video,i2c $USER
bash scripts/build_camera_driver.sh && sudo bash scripts/install_camera_driver.sh
```

Connect the camera to **CAM/DISP 0**. In `/boot/firmware/config.txt`, set `camera_auto_detect=0`, add `dtoverlay=tevs-rpi22,cam0` at the end, and reboot.

**CAM/DISP 1 on the CM5 IO Board** ties the camera's reset line (connector pin 17) high through R8, so the camera never boots (`tevs ... bootup timeout`). Fix: fit both J6 jumpers, wire GPIO 4 (header pin 7) through 330 Ω to R8's pad on the connector side (not its 3.3 V side), and use `dtoverlay=tevs-rpi22,cam1` followed by `dtoverlay=tevs-cam1-reset`. That overlay (installed by the install script) gives the driver GPIO 4 as its reset line.

Rerun the build and install scripts after every kernel update.

## Run

```bash
uv run python camera_API.py
```

Shows 60 s of live RAW10 and saves the last frame as `raw10.pgm`. Quit with Ctrl+C, not Ctrl+Z.
