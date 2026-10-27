#!/usr/bin/env python3
"""Grab N RAW10 frames from the IMX412C and save the last one as <out>.npy + <out>.json (and <out>.png with --png).

Run from the repo root:  uv run python scripts/capture_raw10.py --count 5 --exposure 30000 --png
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # Repo root, so `vc` imports without installing
from PIL import Image  # noqa: E402

from vc import Camera, Mode, save, to_rgb8  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--count", type=int, default=5, help="frames to read; the last one is saved")
parser.add_argument("--width", type=int, default=4032)
parser.add_argument("--height", type=int, default=3040)
parser.add_argument("--binning", type=int, default=0, help="0-5, see vc.imx412.BINNING")
parser.add_argument("--exposure", type=int, default=30000, help="microseconds")
parser.add_argument("--gain", type=int, default=0, help="milli-dB, 6000 is about 2x")
parser.add_argument("--fps", type=float, default=0, help="0 = as fast as the mode allows")
parser.add_argument("--out", default="capture_raw10", help="output path without suffix")
parser.add_argument("--png", action="store_true", help="also write a half-size colour preview")
args = parser.parse_args()

with Camera(Mode(args.width, args.height, binning=args.binning), exposure=args.exposure, gain=args.gain,
            frame_rate=round(args.fps * 1000)) as cam:
    started = time.monotonic()
    for i in range(args.count):
        frame = cam.read()
        print(f"frame {i + 1}: min {frame.min()} max {frame.max()} mean {frame.mean():.1f} of 1023")
    print(f"{args.count / (time.monotonic() - started):.1f} fps measured (including the stats above)")
    info = cam.info()

save(frame, args.out, info)
if args.png:
    Image.fromarray(to_rgb8(frame)).save(f"{args.out}.png")
print(f"saved {Path(args.out).resolve()}.npy/.json" + ("/.png" if args.png else ""))
