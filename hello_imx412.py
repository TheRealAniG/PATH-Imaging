"""Hello world for the Vision Components IMX412C: grab one RAW10 frame, save it raw and as a colour PNG.

Captures with VC's official V4L2 class (vc_capture.py) in 'RG16': the Pi unpacks each 10-bit RGGB
pixel to 16 bits (value << 6), as described in VC's docs/pixelformat.md.
Run: uv run python hello_imx412.py [exposure_us]     (default 30000 = 30 ms)
"""
from pathlib import Path
import re
import subprocess
import sys

import numpy as np
from PIL import Image

import vc_capture as vc

W, H = 4032, 3040  # Full sensor, 10-bit Bayer RGGB
RG16 = int.from_bytes(b"RG16", "little")
exposure = int(sys.argv[1]) if len(sys.argv) > 1 else 30000

for media in sorted(Path("/dev").glob("media*")):  # Media numbering changes between boots
    graph = subprocess.run(["media-ctl", "-d", media, "-p"], capture_output=True, text=True).stdout
    if sensor := re.search(r"entity \d+: (vc_mipi_camera \S+)", graph):
        break
entity = lambda name: subprocess.run(["media-ctl", "-d", media, "-e", name], capture_output=True, text=True).stdout.strip()
subprocess.run(["v4l2-ctl", "-d", entity(sensor[1]), "-c", f"exposure={exposure}"], check=True)

cap = vc.V4L2Capture(entity("rp1-cfe-csi2_ch0"))
cap.open()
cap.set_format(W, H, RG16)
cap.request_buffers(4)
for i in range(len(cap.buffers)):
    cap.queue_buffer(i)
cap.stream_on()
for _ in range(6):  # The first frames after starting (and after an exposure change) are not valid yet
    frame = cap.capture_frame()
cap.close()

raw = frame.view(np.uint16).reshape(H, W) >> 6  # True 10-bit values, 0-1023, RGGB layout
np.save("hello_imx412.npy", raw)

# For viewing only: each 2x2 RGGB block becomes one RGB pixel, then grey-world white balance and gamma
rgb = np.stack([raw[0::2, 0::2], (raw[0::2, 1::2] + raw[1::2, 0::2]) / 2, raw[1::2, 1::2]], 2).astype(np.float32)
rgb *= rgb.mean() / rgb.mean(axis=(0, 1))
rgb = (np.clip(rgb / np.percentile(rgb, 99.5), 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
Image.fromarray(rgb).save("hello_imx412.png")
print(f"IMX412C RAW10: values {raw.min()}..{raw.max()} (mean {raw.mean():.1f} of 1023) at {exposure / 1000:g} ms\n"
      f"  raw: {Path('hello_imx412.npy').resolve()}  preview: {Path('hello_imx412.png').resolve()}")
