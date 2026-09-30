#!/usr/bin/env python3
"""Summarise captures from test_raw8.sh and save the last frame of each as PGM."""
import sys
from pathlib import Path

import numpy as np

out, w, h = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
for name, bpp in (("uyvy", 2), ("y8", 1), ("raw8", 1)):
    f = out / f"{name}.bin"
    data = np.fromfile(f, np.uint8) if f.exists() else np.empty(0, np.uint8)
    size = w * h * bpp
    if data.size < size:
        print(f"{name:5s}: NO FRAMES ({data.size} bytes)")
        continue
    frame = data[-size:].reshape(h, w * bpp)
    y = frame[:, 1::2] if name == "uyvy" else frame  # UYVY: luma is every odd byte
    p = np.percentile(y, [1, 50, 99])
    print(f"{name:5s}: {data.size // size} frames  mean={y.mean():6.1f} std={y.std():5.1f} "
          f"min={y.min()} p1={p[0]:.0f} median={p[1]:.0f} p99={p[2]:.0f} max={y.max()} "
          f"distinct={np.unique(y).size}")
    (out / f"{name}.pgm").write_bytes(f"P5 {w} {h} 255\n".encode() + np.ascontiguousarray(y).tobytes())
