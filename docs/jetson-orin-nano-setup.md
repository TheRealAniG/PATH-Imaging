# Jetson Orin Nano: camera drivers and boot setup

State on 2026-11-10: drivers and overlay are **installed and both cameras stream RAW10**, from `v4l2-ctl` and
from our Python code (`scripts/capture_raw10.py`, `dual_stream.py`, `camera_ui.py` on the display). See "Open problems" for what is left.

Board: Jetson Orin Nano Developer Kit Super, L4T 39.2.1 (JetPack 7.2.1), kernel `6.8.12-1021-tegra`.

## What is on the board

Read over I2C before any driver was installed:

| Connector | Camera | Evidence |
|---|---|---|
| CAM0 (2 lanes) | Vision Components IMX412C, module rev 6 | module ROM at 0x10 says `IMX412C`, sensor at 0x1a |
| CAM1 (4 lanes) | TechNexion TEVS-AR0822 on TEV-RPI22 | chip ID register reads 0x0F56, expander at 0x27 |

CAM0 has only 2 lanes, so the IMX412C tops out near 20 fps at full resolution there (about 40 fps on
CAM1's 4 lanes, which is how the Pi ran it). To swap, move the cables and install with `--imx412-on cam1`.
The board was swapped this way on 2026-11-15: see "After the connector swap" below.

## Build, install, undo

```bash
bash jetson/build.sh              # no sudo; about 20 s; output in .jetson-build/out
sudo bash jetson/install.sh       # modules, overlay, new default boot entry "path-cameras"
sudo reboot
sudo bash jetson/uninstall.sh     # undo: restores extlinux.conf byte for byte
```

`install.sh` copies the existing `primary` boot entry and leaves `primary` itself alone. If the new entry
does not boot, pick `primary` in the boot menu (3 s, monitor or serial console) and run `uninstall.sh`.
`bash jetson/install.sh --root /some/dir` installs into a directory instead, without sudo, to see what it does.

`build.sh` does three things that are easy to get wrong by hand:

- **ABI check.** NVIDIA's header package lacks the generated `nvidia/conftest.h`. One of its macros adds a
  member to `struct camera_common_data`, so a wrong guess builds fine and then corrupts kernel memory.
  `jetson/include/nvidia/conftest.h` is our stand-in. The script builds TechNexion's unmodified driver with
  it and requires the result to match TechNexion's prebuilt `tevs.ko` instruction for instruction.
- **TEVS RAW10.** TechNexion's Jetson driver only outputs UYVY. `jetson/patches/tevs-raw10.patch` is the
  Jetson version of `patches/tevs-raw.patch`: a device-tree mode declared bayer/grbg/10 selects ISP format 0x90.
- **Overlay check.** `jetson/overlay/path-cameras.dts` joins the two vendors' overlays, one camera each.
  `jetson/check_overlay.py` compares every property with the vendors' own files.

## Check after the reboot

```bash
journalctl -k -b | grep -iE "vc_mipi|tevs"     # both drivers should report a successful probe
v4l2-ctl --list-devices                        # expect "vi-output, vc_mipi 10-001a" and "vi-output, tevs 9-0048"
# IMX412C, 5 full frames: the file must be exactly 5 x 4032 x 3040 x 2 = 122572800 bytes
v4l2-ctl -d /dev/videoA -c bypass_mode=0 --set-fmt-video=width=4032,height=3040,pixelformat=RG10 \
    --stream-mmap --stream-count=5 --stream-to=imx412.raw
# TEVS RAW10, 5 frames at 1080p: 5 x 1920 x 1080 x 2 = 20736000 bytes
v4l2-ctl -d /dev/videoB -c bypass_mode=0 -c exposure_mode=0 -c exposure=4000 \
    --set-fmt-video=width=1920,height=1080,pixelformat=BA10 --stream-mmap --stream-count=5 --stream-to=tevs.raw
```

Take `/dev/videoA` and `/dev/videoB` from `--list-devices`. If the TEVS probes but delivers no frames, try
`echo 0x2b | sudo tee /sys/module/tevs/parameters/raw10_mipi_dt` and stream again (see the patch header).

Differences from the Pi to expect:

- One video node per camera does everything: format, controls and frames. No `media-ctl` wiring.
- Formats are `RG10` (IMX412C) and `BA10` (TEVS) instead of `RG16` / `GR16`.
- Pixels sit in the top bits of each 16-bit word, as on the Pi, so `>> 6` gives 0-1023. On the Orin the low
  six bits may repeat the top bits instead of being zero. Confirm on the first real frame.
- IMX412C controls are `exposure` (us), `gain` (mdB) and `frame_rate` (mHz), set on the video node.

## Measured after install

- `vc_mipi 10-001a` is `/dev/video0`, `tevs 9-0048` is `/dev/video1`; controls are on each sensor's sub-device
  node (`/dev/v4l-subdev0` and `/dev/v4l-subdev3`). `find_camera` in `cameras/shared.py` looks all of these up by name.
- Pixel layout: the 10-bit value is in bits 15..6 and the low six bits repeat the top six. `>> 6` gives 0-1023.
- IMX412C alone: 20.05 fps at 4032x3040, the driver's maximum on 2 lanes.
- Both together through `dual_stream.py` (2026-11-10): IMX412C full frame 20 fps, TEVS 1080p 58.5 fps.
  Both read the driver's buffers directly (`open_capture` in `cameras/shared.py`). Before that they piped frames out of `v4l2-ctl`, and
  only 8 to 9.5 of the IMX412C's 20 fps arrived.
- Open: the TEVS can sit in auto exposure while `exposure_mode` reads Manual. It then runs at 30.39 fps (its
  32.9 ms `ae_exposure_upper`) and ignores `exposure`. `v4l2-ctl -d /dev/v4l-subdev1 -c exposure_mode=1` followed
  by `-c exposure_mode=0` cured it; what puts it into that state is not known.
- Open: at 1280x720 the kernel logs `corr_err: discarding frame ... err_data 256` for the TEVS, also with plain `v4l2-ctl`.
- TEVS manual exposure works (`exposure_mode=0`, `exposure` in us): 8x the exposure gave 8x the signal.

## After the connector swap (2026-11-15)

Installed with `sudo bash jetson/install.sh --imx412-on cam1`: IMX412C on CAM1 (4 lanes), TEVS on CAM0 (2 lanes).
Moving the cables without that install gives "camera not found": the overlay says which camera is on which
connector, so each driver probes the wrong one.

- Why only the IMX412C gained: lanes are capacity. It sends 1.5 Gbit/s per lane, so 3.0 Gbit/s on 2 lanes and 6.0
  on 4; a full frame at 40 fps is 4.9 Gbit/s. The TEVS sends 0.8 Gbit/s per lane, 1.6 Gbit/s on 2 lanes, and
  1080p at 60 fps is 1.24 Gbit/s, which already fits.
- IMX412C at 4032x3040: 40.10 fps when the exposure is 20 ms or less, 33.18 fps at the 30 ms starting exposure (a
  frame cannot be shorter than its exposure).
- Python receives all 40 since the frame copy runs in four bands on four threads (`unpack` in
  `cameras/shared.py`): 9 ms a frame instead of 24 ms. Before that 27 fps arrived and the driver skipped the rest.
  Both cameras together, no window: TEVS 1080p 60 fps and IMX412C 40 fps, none skipped in 10 s.
- TEVS on 2 lanes: 1080p 60.00 fps and 4K 15.00 fps as asked, but 2560x1440 gives 17.55 fps when 30 or 20 is
  asked. That size was not measured here before the swap.
- Recording cannot follow 40 fps at full size: the GPU encodes about 20 colour frames a second in 25W mode, and a
  4 s test dropped 63 of 140. The status line counts them.

## Recording (2026-11-11)

The Record button in `camera_ui.py` saves every frame of both cameras, lossless, as `.j2ks` files beside the
captures. `recording.py` reads them back (`python recording.py file.j2ks`, `--check`, `--png N`, `--mp4`).
Right after Stop it also makes an `.mp4` of the live view beside each one, to watch (about 8 pictures a second, at
most 1024 wide); `--mp4` replaces that with a video of every frame.

- Each frame is compressed on the GPU with NVIDIA's nvJPEG2000 in its lossless High-Throughput mode. NVIDIA lists
  it for Orin from CUDA 13.2; the driver here is CUDA 13.2, and the three pip packages below bring the rest, so
  nothing is installed system-wide. The Orin Nano has no hardware video encoder, and lossless H.265 or FFV1 on
  the CPU reached 4 and 29 fps at 1080p.
- Measured live: FL 1080p 60 fps at 10 ms a frame, colour 4032x3040 20 fps at about 40 ms, files 12-37 % and
  33 % of the 16-bit size, about 12 GB a minute for both. Decoded frames were compared with the camera's and
  were identical.
- Power mode matters. A minute in MAXN_SUPER (`sudo nvpmodel -m 2`) lost no frame. In 25W, through the UI with
  other programs busy, the colour camera lost 2-5 % (the capture loop is late and the driver skips a frame);
  the FL camera lost almost none. The UI's status line and `python recording.py file.j2ks` both count them.
- The FL camera now gets 8 driver buffers instead of 4: with 4 it lost 67 of 3600 frames while recording.
- Memory: the board has 7.5 GB shared with the GPU and no swap. Recording itself holds about 150 MB.

## Open problems

- **Kernel faults while the IMX412C streams.** `journalctl -k` fills with `arm-smmu ... Unhandled context
  fault ... iova=0x00000000` and `tegra-mc ... viw: secure write ... VPR violation` / `vifalr ... EMEM address
  decode error`. Never with the TEVS alone. Frames look intact. The log shows 10 faults then a 5 s gap, which
  is the kernel's rate limit, so the real rate is about one per frame: the capture hardware writes something
  to address 0 on every frame. Suspect: the embedded-metadata buffer, since the overlay declares 2 embedded
  lines for this sensor and 0 for the TEVS. Declaring 0 lines is not the fix (tried 2026-11-10 with
  `IMX412_METADATA_H=0 bash jetson/build.sh`): the faults stop, but only because every frame is thrown away
  (`corr_err: discarding frame 0 ... err_data 16384`, 0 fps). The sensor does send the 2 lines, so the fix has
  to be on the buffer side, in NVIDIA's capture driver. Keep the default of 2.
- **Pi code paths** in `cameras/` (names ending in `_pi`), `dual_stream.py` and `camera_ui.py` have not been
  retested on a Pi since the Jetson port, nor since the code was split into one file per camera (2026-11-15). A
  simulated run shows the split issues the same `media-ctl` / `v4l2-ctl` commands and ioctls as before.
- **`uv sync` fails here**: `pyproject.toml` wants Python 3.13 and Ubuntu 24.04 has 3.12 (the GStreamer and
  GTK bindings are tied to it). `.venv` was made by hand with `uv venv --python /usr/bin/python3
  --system-site-packages` plus `uv pip install numpy pillow opencv-python-headless` and, for
  recording, `uv pip install nvidia-nvjpeg2k-cu13==0.12.0.58 nvidia-nvimgcodec-cu13==0.10.0.9
  nvidia-cuda-runtime==13.2.86`. Use `.venv/bin/python`, not `uv run`.
- **IMX412C black level, trigger, I/O mode, binning and sensor crop** need Vision Components' patches to
  NVIDIA's `tegra-camera` module, which we do not apply. Smaller sizes are software crops of the full frame.
  Measure the black level with the lens capped; do not assume the Pi's 64 counts.
- **`v4l-utils`** is unpacked under `~/.local` for this user only. `sudo apt install v4l-utils` is cleaner.

## After a kernel update

Rerun `build.sh` and `install.sh`. If the build stops at the conftest or ABI check, NVIDIA's headers
changed: read which macros `media/camera_common.h` and `media/mc_common.h` test, work out each one from
`scripts/conftest/conftest.sh` in NVIDIA's `public_sources.tbz2` for that release, and update
`jetson/include/nvidia/conftest.h`. Do not install modules that fail the ABI check.
