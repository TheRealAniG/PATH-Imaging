"""Window showing both cameras live, with size and frame rate pickers and the measured frame rate.

  FL:    TechNexion TEVS-AR0822-M on CAM/DISP 0 (mono RAW10)
  Color: Vision Components IMX412C on CAM/DISP 1 (colour RAW10, full 4032x3040 sensor at 30 ms, smaller sizes are centre
         crops; the live view is white balanced and brightness stretched like hello_imx412.py)

Picking a size or frame rate restarts that camera. Starting modes are CAMERAS in dual_stream.py.
Capture saves each camera's latest frame, full size, as ~/Desktop/PATH-captures/<sensor>/<time>/<size>_<fps>.png (16-bit,
colour demosaiced, no white balance) and .raw (16-bit little-endian 10-bit values 0-1023, row by row, no header).
Close the window or press Ctrl+C to stop.
Run: uv run python camera_ui.py
"""
from pathlib import Path
import signal
import threading
import time

import cv2
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst, Gtk

from dual_stream import CAMERAS, run, start

TITLES = {"fl": "FL: TEVS-AR0822-M (mono, CAM0)", "color": "Color: IMX412C (CAM1)"}
SENSORS = {"fl": "TEVS-AR0822-M", "color": "IMX412C"}
CAPTURES = Path.home() / "Desktop" / "PATH-captures"  # Created on the first capture
TEVS = [60, 30, 20, 15, 10, 5]
MODES = {  # Sizes offered and the frame rates each allows (0 = as fast as the sensor can)
    "fl": {"640x480": TEVS, "1280x720": TEVS, "1920x1080": TEVS, "2560x1440": TEVS[1:], "3840x2160": TEVS[3:]},
    "color": dict.fromkeys(["640x480", "1280x960", "1920x1080", "3840x2160", "4032x3040"], [0, 60, 30, 15, 10, 5]),
}

Gst.init(None)
window = Gtk.Window(title="PATH Imaging cameras")
window.set_default_size(1400, 640)
page, row, bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=12), Gtk.Box(spacing=12, homogeneous=True), Gtk.Box(spacing=12)
button, status = Gtk.Button(label="Capture"), Gtk.Label()
bar.pack_start(button, False, False, 0)
bar.pack_start(status, False, False, 0)
page.pack_start(row, True, True, 0)
page.pack_start(bar, False, False, 0)
window.add(page)
cams, threads, ui = {}, {}, {}


def stop_camera(name):
    cam = cams.pop(name)
    cam["stream"].kill()  # Its capture thread ends when the pipe closes
    cam["stream"].wait()
    threads[name].join()
    cam["window"].set_state(Gst.State.NULL)


def restart(name):
    sizes, rates, slot, info = ui[name]
    if name in cams:
        stop_camera(name)
    for child in slot.get_children():
        slot.remove(child)
    width, height = map(int, sizes.get_active_id().split("x"))
    cam = start(name, *CAMERAS[name][:3], width, height, int(rates.get_active_id()), sink="gtksink name=sink sync=false")
    slot.pack_start(cam["window"].get_by_name("sink").get_property("widget") if cam else Gtk.Label(label="Camera not found"),
                    True, True, 0)
    slot.show_all()
    info.set_text("")
    if cam:
        cam["window"].set_state(Gst.State.PLAYING)  # Only once the video widget is inside the window
        cam["counted"] = (0, time.monotonic())
        cams[name], threads[name] = cam, threading.Thread(target=run, args=(name, cam, threading.Event()))
        threads[name].start()


def pick_size(sizes, name):  # Offer the frame rates this size allows, keeping the current one if it is among them
    rates = ui[name][1]
    keep = rates.get_active_id() or str(CAMERAS[name][5] or 0)
    rates.remove_all()  # Leaves nothing picked, which pick_rate ignores
    for fps in MODES[name][sizes.get_active_id()]:
        rates.append(str(fps), f"{fps} fps" if fps else "max")
    if not rates.set_active_id(keep):
        rates.set_active(0)


def pick_rate(rates, name):
    if rates.get_active_id():
        restart(name)


def capture(button):  # Takes whatever frame each camera has just delivered; saving runs in the background
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    for name, cam in cams.items():
        if cam["last"] is not None:
            height, width = cam["size"]
            fps = ui[name][1].get_active_id()
            path = Path(CAPTURES, SENSORS[name], stamp, f"{width}x{height}_{fps if fps != '0' else 'max'}fps")
            threading.Thread(target=save, args=(name, cam["last"], path)).start()
    status.set_text(f"Saving {stamp} ...")


def save(name, frame, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    (frame >> 6).tofile(path.with_suffix(".raw"))  # True 10-bit values
    cv2.imwrite(str(path.with_suffix(".png")), frame if name == "fl" else cv2.cvtColor(frame, cv2.COLOR_BayerBG2BGR))  # RGGB
    GLib.idle_add(status.set_text, f"Saved {CAPTURES}/*/{path.parent.name}/")


def update_info():
    for name, cam in cams.items():
        frames, then = cam["counted"]
        now = time.monotonic()
        height, width = cam["size"]
        ui[name][3].set_text(f"{width} x {height}  ·  RAW10  ·  {(cam['frames'] - frames) / (now - then):.1f} fps")
        cam["counted"] = (cam["frames"], now)
    return True  # Keep updating


for name, spec in CAMERAS.items():
    column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    title = Gtk.Label()
    title.set_markup(f"<b>{TITLES.get(name, name)}</b>")
    sizes, rates, slot, info = Gtk.ComboBoxText(), Gtk.ComboBoxText(), Gtk.Box(), Gtk.Label()
    for size in MODES[name]:
        sizes.append(size, size.replace("x", " x "))
    controls = Gtk.Box(spacing=6)
    for widget in (Gtk.Label(label="Size"), sizes, Gtk.Label(label="Frame rate"), rates):
        controls.pack_start(widget, False, False, 0)
    controls.pack_end(info, False, False, 0)
    for widget, expand in ((title, False), (slot, True), (controls, False)):
        column.pack_start(widget, expand, expand, 0)
    row.pack_start(column, True, True, 0)
    ui[name] = sizes, rates, slot, info
    sizes.connect("changed", pick_size, name)
    rates.connect("changed", pick_rate, name)
    sizes.set_active_id(f"{spec[3]}x{spec[4]}")  # Fills in the frame rates, which starts the camera

button.connect("clicked", capture)
GLib.timeout_add(1000, update_info)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)
window.connect("destroy", Gtk.main_quit)
window.show_all()
Gtk.main()

for name in list(cams):
    stop_camera(name)
