#!/usr/bin/env python3
"""RAW8 preview: same as `python3 camera_preview.py --raw8`.

The camera's ISP is bypassed, so the image is darker, noisier and has dark
corners (no lens-shading correction).
"""
import camera_preview

if __name__ == "__main__":
    camera_preview.main(raw8=True)
