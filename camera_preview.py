#!/usr/bin/env python3
"""720p preview for a TechNexion TEVS camera on the CM5."""
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst, Gtk

WIDTH, HEIGHT = 1280, 720


def media_ctl(device, *args):
    return subprocess.check_output(
        ["media-ctl", "-d", str(device), *args], text=True, timeout=10
    ).strip()


def main():
    if int(Path("/proc/sys/kernel/tainted").read_text()) & 128:
        raise RuntimeError("Kernel crash detected. Save dmesg and reboot before capture.")
    cameras = []
    for device in sorted(Path("/dev").glob("media*")):
        match = re.search(r"entity \d+: (tevs [^\n]+?) \(", media_ctl(device, "-p"))
        if match:
            cameras.append((device, match[1]))
    if len(cameras) != 1:
        raise RuntimeError(f"Expected one TEVS camera, found {len(cameras)}.")
    device, sensor = cameras[0]
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/path-imaging-{os.getuid()}"))
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (runtime / f"path-imaging-{device.name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)  # Don't suspend an open camera.
        Gst.init(None)
        if not Gtk.init_check()[0]:
            raise RuntimeError("Run from a terminal on the Pi desktop.")
        video = media_ctl(device, "-e", "rp1-cfe-csi2_ch0")
        if not re.fullmatch(r"/dev/video\d+", video):
            raise RuntimeError(f"Invalid capture device: {video}")
        media_ctl(device, "-l", '"csi2":4 -> "rp1-cfe-csi2_ch0":0 [1]')
        media_ctl(device, "-V", f'"{sensor}":0 [fmt:UYVY8_1X16/{WIDTH}x{HEIGHT} '
                  'field:none colorspace:srgb xfer:srgb ycbcr:601]')
        show_video(video)


def show_video(video):
    pipeline = Gst.parse_launch(
        f"v4l2src device={video} ! video/x-raw,format=UYVY,width={WIDTH},height={HEIGHT} ! "
        "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
        "videoconvert ! gtksink name=display sync=false"
    )
    window = Gtk.Window(title="PATH Imaging — Camera")
    window.set_default_size(WIDTH, HEIGHT)
    window.add(pipeline.get_by_name("display").get_property("widget"))
    errors = []

    def close(*_):
        if Gtk.main_level():
            Gtk.main_quit()
        return False

    def message(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            errors.append(msg.parse_error()[0].message)
            close()
        elif msg.type == Gst.MessageType.EOS:
            close()

    window.connect("destroy", close)
    window.connect("key-press-event", lambda _, e: close() if e.keyval in (65307, 113, 81) else False)
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", message)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, close)
    try:
        window.show_all()
        print(f"Opening {video}. Close with Q, Esc, or Ctrl+C.", flush=True)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not start capture.")
        Gtk.main()
    finally:
        pipeline.set_state(Gst.State.NULL)
        bus.remove_signal_watch()
        window.destroy()
    if errors:
        raise RuntimeError(errors[0])


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError, GLib.Error) as error:
        sys.exit(f"Camera preview: {error}")
