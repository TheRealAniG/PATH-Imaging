"""Capture monochrome images without a display: uv run camera_capture.py."""
import argparse
from datetime import datetime
from pathlib import Path
import time

import camera_API as vx


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=positive_int, default=30)
    parser.add_argument("--width", type=positive_int, default=1920)
    parser.add_argument("--height", type=positive_int, default=1080)
    parser.add_argument("--fps", type=positive_int, default=60)
    parser.add_argument("--raw8", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    mode = "raw8" if args.raw8 else "uyvy"
    output = args.output_dir or Path("captures") / (
        mode + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    )
    count, names = vx.VxDiscoverCameraDevices()
    if count != 1:
        raise RuntimeError(f"Expected one camera, found {count}")
    camera = vx.VxInitialCameraDevice(0)
    if vx.VxOpen(camera) != 0:
        raise RuntimeError(f"Could not open {names[0]}")
    streaming = False
    try:
        result, formats = vx.VxGetFormatList(camera)
        if result != 0:
            raise RuntimeError(f"Could not list formats: {result}")
        wanted = vx.VX_IMAGE_FORMAT_RAW8 if args.raw8 else vx.VX_IMAGE_FORMAT.VX_IMAGE_FORMAT_UYVY
        fmt = next((f for f in formats if f.format == wanted and
                    (f.width, f.height, f.framerate) ==
                    (args.width, args.height, args.fps)), None)
        if fmt is None:
            raise RuntimeError(f"Unsupported {mode} mode: {args.width}x{args.height}@{args.fps}")
        if vx.VxSetFormat(camera, fmt) != 0:
            raise RuntimeError("Could not set camera format")
        output.mkdir(parents=True, exist_ok=False)
        if vx.VxStartStreaming(camera) != 0:
            raise RuntimeError("Could not start streaming")
        streaming = True
        expected = fmt.width * fmt.height * (1 if args.raw8 else 2)
        header = f"P5\n{fmt.width} {fmt.height}\n255\n".encode()
        saved, timeouts, warmup = 0, 0, True
        start = time.monotonic()
        while saved < args.frames:
            result, image = vx.VxGetImage(camera, 2500, fmt)
            if result == vx.VX_CAPTURE_RESULT.VX_TIMEOUT:
                timeouts += 1
                if timeouts >= 3:
                    raise TimeoutError("Camera timed out three times in a row")
                continue
            if result != vx.VX_CAPTURE_RESULT.VX_SUCCESS:
                raise RuntimeError(f"Capture failed: {result}")
            if image.size != expected:
                raise RuntimeError(f"Incomplete frame: {image.size} bytes, expected {expected}")
            timeouts = 0
            if warmup:
                warmup = False  # First frame after startup may be blank.
                continue
            gray = (image.reshape(fmt.height, fmt.width) if args.raw8 else
                    image.reshape(fmt.height, fmt.width * 2)[:, 1::2])
            (output / f"frame-{saved:06d}.pgm").write_bytes(header + gray.tobytes())
            saved += 1
        elapsed = time.monotonic() - start
        print(f"Saved {saved} {fmt.width}x{fmt.height} {mode} frames in {elapsed:.2f}s "
              f"(requested {fmt.framerate} fps) to {output.resolve()}")
    finally:
        try:
            if streaming:
                vx.VxStopStreaming(camera)
        finally:
            vx.VxClose(camera)


if __name__ == "__main__":
    main()
