# VC MIPI IMX412C on the CM5: driver and boot setup

Condensed from Vision Components' [Readme](https://github.com/VC-MIPI-modules/vc_mipi_raspi/blob/53eba8e/Readme.md)
and [FAQ](https://github.com/VC-MIPI-modules/vc_mipi_raspi/blob/53eba8e/docs/faq.md), with this project's values.
The team Pi already has all of this done; it is here for a fresh Pi and for after a kernel update.

## 1. Install the driver package

Download the **bcm2712** `.deb` (Pi 5 / CM5) from VC's [releases](https://github.com/VC-MIPI-modules/vc_mipi_raspi/releases):

```bash
sudo apt install ./vc-mipi-driver-bcm2712_*_arm64.deb
```

It installs the `vc_mipi_camera` kernel module through DKMS, the boot file below, the `vc-config` tool and the
`set_rpi5_pipeline` udev helper that wires the media pipeline after boot.

## 2. Configure the camera port

The IMX412C is on **CAM/DISP 1**, 4 lanes, and it is a Sony sensor. The BCM2712 defaults are 4 lanes and
**Onsemi**, so the manufacturer line is mandatory; with the wrong one the driver binds the wrong I2C address
and probing fails (`vc_mod_setup(): Unable to get module I2C client`). In
`/boot/firmware/config_vc-mipi-driver-bcm2712.txt`:

```
dtoverlay=vc-mipi-bcm2712-cam1
dtparam=cam1_lanes4
dtparam=cam1_manu_sony
dtparam=cam1_libcamera_off
dtoverlay=vc-mipi-common-memory-contiguous
```

Leave the `cam0` lines commented out: the TEVS camera is on CAM/DISP 0. `cam1_libcamera_off` keeps the Pi's ISP
out of the way, so frames are raw and `exposure` is in microseconds. Running both cameras needs, at the end of
`/boot/firmware/config.txt`, a blank `dtoverlay=` followed by `dtparam=cam0_reg`, because the VC overlay disables
the regulator the TEVS uses for reset. Then reboot.

## 3. Check the driver found the camera

```bash
dmesg | grep vc_mipi_camera        # "Probe successful", no lane or I2C errors
media-ctl -p -d /dev/media1        # an entity "vc_mipi_camera 0-001a" linked to "csi2"
v4l2-ctl --all -d /dev/v4l-subdev5 # its controls: exposure, analogue_gain, black_level, frame_rate, ...
```

On the team Pi the camera is `vc_mipi_camera 0-001a` on `/dev/media1`, subdevice `/dev/v4l-subdev5`, frames on
`/dev/video8`, but the numbers move between boots, which is why `vc.find_camera()` looks them up. VC's
`tools/self_check.sh` (in their repo) runs the same checks plus a streaming test.

## 4. Manual pipeline once, by hand

This is VC's `docs/cropping.md` with our values, and exactly what `vc.imx412.configure()` does:

```bash
M=/dev/media1; S="vc_mipi_camera 0-001a"
media-ctl -d $M -V "'$S':0 [fmt:SRGGB10_1X10/4032x3040 field:none colorspace:srgb]"
media-ctl -d $M -V "'$S':0 [crop:(0,0)/4032x3040]"
media-ctl -d $M -V "'csi2':0 [fmt:SRGGB10_1X10/4032x3040 field:none colorspace:srgb]"
media-ctl -d $M -V "'csi2':4 [fmt:SRGGB10_1X10/4032x3040 field:none colorspace:srgb]"
media-ctl -d $M -l "'csi2':4 -> 'rp1-cfe-csi2_ch0':0 [1]"
v4l2-ctl -d /dev/v4l-subdev5 -c exposure=30000
v4l2-ctl -d /dev/video8 --set-fmt-video=width=4032,height=3040,pixelformat=RG16 --stream-mmap --stream-count=5 --stream-to=frames.raw
ls -l frames.raw                   # exactly 5 x 4032 x 3040 x 2 = 122572800 bytes
```

`RG16` needs v4l-utils 1.30 or newer (Bookworm's 1.22 cannot unpack raw Bayer; the team Pi has 1.30.1).

## 5. Capture with our code

```bash
uv run python scripts/capture_raw10.py --count 5 --png
```

## After a kernel update

The DKMS module is rebuilt automatically on most updates. If `dmesg` shows
`vc_mipi_camera: disagrees about version of module layout`, rebuild it by hand:

```bash
sudo dkms status
sudo dkms install vc-mipi-driver-bcm2712/<version from status> -k $(uname -r) --force
```

The TEVS driver has its own rebuild steps in the README.
