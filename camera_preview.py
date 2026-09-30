#!/usr/bin/env python3
"""720p preview for the TEVS-AR0822-M, using camera_API.

Usage: python3 camera_preview.py [--raw8]
Close with Q, Esc, the window close button, or Ctrl+C.
"""
import signal
import sys
import threading

from camera_gui import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, GLib, Gst, Gtk

import camera_API as vx

WIDTH, HEIGHT, FPS = 1920, 1080, 60


def open_camera(image_format):
    result, camera_list = vx.VxDiscoverCameraDevices()
    if result != 1:
        sys.exit(f"Expected one TEVS camera, found {result}. Is the tevs driver loaded?")
    camera = vx.VxInitialCameraDevice(0)
    if vx.VxOpen(camera) != 0:
        sys.exit(f"Could not open {camera_list[0]}.")
    result, format_list = vx.VxGetFormatList(camera)
    fmt = next((f for f in format_list if f.format == image_format
                and (f.width, f.height, f.framerate) == (WIDTH, HEIGHT, FPS)), None)
    if fmt is None or vx.VxSetFormat(camera, fmt) != 0:
        sys.exit(f"{WIDTH}x{HEIGHT}@{FPS} not available in this format. RAW8 needs the "
                 "patched driver: bash scripts/build_camera_driver.sh && "
                 "sudo bash scripts/install_camera_driver.sh")
    if vx.VxStartStreaming(camera) != 0:
        sys.exit("Could not start streaming. Is another camera app open?")
    return camera, fmt


def to_gray(image, fmt):
    """The AR0822-M is mono: RAW8 is already gray, UYVY carries it in the Y bytes."""
    if fmt.format == vx.VX_IMAGE_FORMAT.VX_IMAGE_FORMAT_UYVY:
        return image.reshape(fmt.height, fmt.width * 2)[:, 1::2]
    return image.reshape(fmt.height, fmt.width)


def main(raw8=False):
    image_format = vx.VX_IMAGE_FORMAT_RAW8 if raw8 else vx.VX_IMAGE_FORMAT.VX_IMAGE_FORMAT_UYVY
    camera, fmt = open_camera(image_format)
    error = None
    running = threading.Event()
    running.set()

    # Frames from camera_API are pushed into a GStreamer display that scales to the window.
    Gst.init(None)
    display = Gst.parse_launch(
        f"appsrc name=src is-live=true format=time do-timestamp=true "
        f"caps=video/x-raw,format=GRAY8,width={fmt.width},height={fmt.height},framerate={fmt.framerate}/1 ! "
        "queue max-size-buffers=2 leaky=downstream ! videoconvert ! gtksink name=sink sync=false"
    )
    source = display.get_by_name("src")
    window = Gtk.Window(title=f"PATH Imaging — Camera ({'RAW8' if raw8 else 'UYVY'})")
    window.set_default_size(WIDTH, HEIGHT)
    window.add(display.get_by_name("sink").get_property("widget"))

    def capture():
        nonlocal error
        while running.is_set():
            result, image = vx.VxGetImage(camera, 2500, fmt)
            if result == vx.VX_CAPTURE_RESULT.VX_TIMEOUT:
                continue
            if result != vx.VX_CAPTURE_RESULT.VX_SUCCESS:
                error = result.name
                GLib.idle_add(Gtk.main_quit)
                return
            source.emit("push-buffer", Gst.Buffer.new_wrapped(to_gray(image, fmt).tobytes()))

    window.connect("destroy", lambda *_: Gtk.main_quit())
    window.connect("key-press-event",
                   lambda _, e: Gtk.main_quit() if e.keyval in (Gdk.KEY_q, Gdk.KEY_Q, Gdk.KEY_Escape) else None)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)
    worker = threading.Thread(target=capture, daemon=True)
    try:
        window.show_all()
        display.set_state(Gst.State.PLAYING)
        worker.start()
        Gtk.main()
    finally:
        running.clear()
        if worker.is_alive():
            worker.join(timeout=3)
        display.set_state(Gst.State.NULL)
        vx.VxStopStreaming(camera)
        vx.VxClose(camera)
    if error:
        sys.exit(f"Capture failed: {error}")


if __name__ == "__main__":
    main(raw8="--raw8" in sys.argv[1:])
