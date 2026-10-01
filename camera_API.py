"""pyvizionsdk plus RAW10 for the TEVS-AR0822-M on a Raspberry Pi CM5.

Use like pyvizionsdk (import camera_API as vx; vx.VxOpen(...)). The SDK lists RAW modes as
VX_IMAGE_FORMAT_NONE but can only stream UYVY, so RAW10 is streamed with v4l2-ctl instead.
RAW10 frames come back as flat uint16 arrays of 0-1023.
Run on its own for a 10 s live RAW10 view: python camera_API.py
"""
from pathlib import Path
import subprocess

import vizion_cm5  # noqa: F401  CM5 workaround, must come before pyvizionsdk
import numpy as np
import pyvizionsdk
from pyvizionsdk import VX_CAPTURE_RESULT, VX_IMAGE_FORMAT

VX_IMAGE_FORMAT_RAW10 = VX_IMAGE_FORMAT.VX_IMAGE_FORMAT_NONE
_raw10 = {}   # id(vxcam) -> RAW10 format set
_stream = {}  # id(vxcam) -> v4l2-ctl streaming it


def __getattr__(name):  # Everything else is the SDK's own
    return getattr(pyvizionsdk, name)


def VxSetFormat(vxcam, fmt):
    _raw10.pop(id(vxcam), None)
    if fmt.format != VX_IMAGE_FORMAT_RAW10:
        return pyvizionsdk.VxSetFormat(vxcam, fmt)
    _raw10[id(vxcam)] = fmt
    size = f"fmt:Y10_1X10/{fmt.width}x{fmt.height}"
    for pad in (f'"tevs 10-0048":0 [{size}@1/{fmt.framerate} field:none]',
                f'"csi2":0 [{size} field:none]', f'"csi2":4 [{size} field:none]'):
        subprocess.run(["media-ctl", "-d", "/dev/media0", "-V", pad])
    return 0


def VxStartStreaming(vxcam):
    fmt = _raw10.get(id(vxcam))
    if fmt is None:
        return pyvizionsdk.VxStartStreaming(vxcam)
    _stream[id(vxcam)] = subprocess.Popen(  # 'Y16 ': the Pi unpacks each 10-bit pixel into 16 bits
        ["v4l2-ctl", "-d", "/dev/video0", "--stream-mmap", "--stream-to=-",
         f"--set-fmt-video=width={fmt.width},height={fmt.height},pixelformat=Y16 "],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True)  # Ctrl+C in the terminal must not cut a frame in half
    _stream[id(vxcam)].stdout.read(fmt.width * fmt.height * 2)  # First frame is blank
    return 0


def VxGetImage(vxcam, timeout, fmt):
    if id(vxcam) not in _stream:
        return pyvizionsdk.VxGetImage(vxcam, timeout, fmt)
    frame = _stream[id(vxcam)].stdout.read(fmt.width * fmt.height * 2)
    return VX_CAPTURE_RESULT.VX_SUCCESS, np.frombuffer(frame, np.uint16) >> 6  # 10 bits sit at the top


def VxStopStreaming(vxcam):
    if id(vxcam) not in _stream:
        return pyvizionsdk.VxStopStreaming(vxcam)
    _stream.pop(id(vxcam)).kill()
    return 0


if __name__ == "__main__":  # Show RAW10 live for 10 s, then save the last frame as raw10.pgm
    import time
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    result, camera_list = pyvizionsdk.VxDiscoverCameraDevices()
    vxcam = pyvizionsdk.VxInitialCameraDevice(0)
    pyvizionsdk.VxOpen(vxcam)
    result, format_list = pyvizionsdk.VxGetFormatList(vxcam)
    fmt = next(f for f in format_list if f.format == VX_IMAGE_FORMAT_RAW10
               and (f.width, f.height, f.framerate) == (1280, 720, 30))
    VxSetFormat(vxcam, fmt)
    VxStartStreaming(vxcam)
    Gst.init(None)
    window = Gst.parse_launch(
        f"appsrc name=src is-live=true format=time do-timestamp=true caps=video/x-raw,format=GRAY16_LE,"
        f"width={fmt.width},height={fmt.height},framerate={fmt.framerate}/1 ! videoconvert ! autovideosink sync=false")
    window.set_state(Gst.State.PLAYING)
    frames, end = 0, time.monotonic() + 10
    while time.monotonic() < end:
        result, image = VxGetImage(vxcam, 2500, fmt)
        window.get_by_name("src").emit("push-buffer", Gst.Buffer.new_wrapped((image << 6).tobytes()))
        frames += 1
    window.set_state(Gst.State.NULL)
    VxStopStreaming(vxcam)
    pyvizionsdk.VxClose(vxcam)
    Path("raw10.pgm").write_bytes(f"P5 {fmt.width} {fmt.height} 1023\n".encode() + image.astype(">u2").tobytes())
    print(f"{camera_list[0]}: {frames} RAW10 frames in 10 s, last one saved to {Path('raw10.pgm').resolve()}")
