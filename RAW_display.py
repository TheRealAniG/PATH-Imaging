"""Show RAW8 (default) or UYVY frames live, 1:1 pixels.

RAW8: values exactly as the sensor sent them. UYVY: the camera's ISP output, converted to RGB for display.
When the window is closed, the last frame is saved to raw_frame.raw / uyvy_frame.raw (the exact bytes
received, no header) and to raw_frame.png / uyvy_frame.png (viewable).

Run: uv run RAW_display.py [--uyvy]
"""
from pathlib import Path
import signal
import sys

import camera_API as vx  # First: it restarts Python with the CM5 workaround
from camera_gui import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gtk
import numpy as np
from PIL import Image

UYVY = "--uyvy" in sys.argv[1:]
MODE = "UYVY" if UYVY else "RAW8"
BYTES_PER_PIXEL = 2 if UYVY else 1

result, camera_list = vx.VxDiscoverCameraDevices()
vxcam = vx.VxInitialCameraDevice(0)
vx.VxOpen(vxcam)
result, format_list = vx.VxGetFormatList(vxcam)
wanted = vx.VX_IMAGE_FORMAT.VX_IMAGE_FORMAT_UYVY if UYVY else vx.VX_IMAGE_FORMAT_RAW8
fmt = next(f for f in format_list if f.format == wanted and (f.width, f.height, f.framerate) == (1280, 720, 30))
vx.VxSetFormat(vxcam, fmt)
vx.VxStartStreaming(vxcam)

window = Gtk.Window(title=MODE)
picture = Gtk.Image()
window.add(picture)
last = {"image": None, "rgb": None, "frames": 0}


def to_rgb(image):
    if not UYVY:  # Same value in R, G and B: no scaling, gamma or contrast
        raw = image.reshape(fmt.height, fmt.width)
        return np.repeat(raw[:, :, None], 3, axis=2)
    # UYVY: bytes U0 Y0 V0 Y1 per pixel pair; each U/V is shared by 2 pixels. BT.601 full-range to RGB.
    uyvy = image.reshape(fmt.height, fmt.width // 2, 4).astype(np.float32)
    y = uyvy[:, :, [1, 3]].reshape(fmt.height, fmt.width)
    u = np.repeat(uyvy[:, :, 0], 2, axis=1) - 128
    v = np.repeat(uyvy[:, :, 2], 2, axis=1) - 128
    rgb = np.stack([y + 1.402 * v, y - 0.344 * u - 0.714 * v, y + 1.772 * u], axis=2)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def show_next_frame():
    result, image = vx.VxGetImage(vxcam, 2500, fmt)
    if image.size != fmt.width * fmt.height * BYTES_PER_PIXEL:
        print("Incomplete frame: another camera program has the camera (a paused Ctrl+Z run?). Check: jobs")
        Gtk.main_quit()
        return False
    rgb = to_rgb(image)
    picture.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(rgb.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8, fmt.width, fmt.height, fmt.width * 3))
    last["image"], last["rgb"], last["frames"] = image, rgb, last["frames"] + 1
    window.set_title(f"{MODE} frame {last['frames']}: min {rgb.min()}, max {rgb.max()}, mean {rgb.mean():.1f}")
    return True  # Keep going


GLib.idle_add(show_next_frame)
window.connect("destroy", Gtk.main_quit)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)  # Ctrl+C
window.show_all()
Gtk.main()

vx.VxStopStreaming(vxcam)
vx.VxClose(vxcam)
if last["image"] is None:
    raise SystemExit
name = "uyvy_frame" if UYVY else "raw_frame"
Path(f"{name}.raw").write_bytes(last["image"].tobytes())  # Exact bytes received, no header
Image.fromarray(last["rgb"] if UYVY else last["rgb"][:, :, 0]).save(f"{name}.png")  # Viewable
print(f"Showed {last['frames']} {MODE} frames. Last one saved to {Path(name + '.raw').resolve()} "
      f"({fmt.width}x{fmt.height}, {BYTES_PER_PIXEL} byte(s)/pixel, no header) and {name}.png")
