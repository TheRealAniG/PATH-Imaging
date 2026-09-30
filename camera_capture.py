"""Capture monochrome images and validation metadata: uv run camera_capture.py."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

import camera_API as vx
import numpy as np
from capture_metadata import camera_snapshot, provenance, timing_summary, utc_now


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
    frames = []
    metadata = {"schema_version": 1, "capture_started_utc": utc_now(),
                "camera_name": str(names[0]), "format": mode.upper(), "container": "PGM",
                "width": args.width, "height": args.height, "bit_depth": 8,
                "requested_fps": args.fps, "requested_frames": args.frames,
                "provenance": provenance()}
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
        metadata["before_streaming"] = camera_snapshot(camera)
        stream_start = time.monotonic_ns()
        if vx.VxStartStreaming(camera) != 0:
            raise RuntimeError("Could not start streaming")
        streaming = True
        startup_ns = time.monotonic_ns() - stream_start
        expected = fmt.width * fmt.height * (1 if args.raw8 else 2)
        consecutive_timeouts, total_timeouts, warmup = 0, 0, True
        start = time.monotonic_ns()
        while len(frames) < args.frames:
            read_start = time.monotonic_ns()
            result, image = vx.VxGetImage(camera, 2500, fmt)
            received = time.monotonic_ns()
            if result == vx.VX_CAPTURE_RESULT.VX_TIMEOUT:
                consecutive_timeouts += 1
                total_timeouts += 1
                if consecutive_timeouts >= 3:
                    raise TimeoutError("Camera timed out three times in a row")
                continue
            if result != vx.VX_CAPTURE_RESULT.VX_SUCCESS:
                raise RuntimeError(f"Capture failed: {result}")
            if image.size != expected:
                raise RuntimeError(f"Incomplete frame: {image.size} bytes, expected {expected}")
            consecutive_timeouts = 0
            if warmup:
                warmup = False
                continue
            gray = (image.reshape(fmt.height, fmt.width) if args.raw8 else
                    image.reshape(fmt.height, fmt.width * 2)[:, 1::2])
            frames.append({"payload": gray.tobytes(), "received_monotonic_ns": received,
                           "received_utc": utc_now(), "read_duration_ms": (received - read_start) / 1e6,
                           "received_bytes": int(image.size)})
        acquisition_ns = time.monotonic_ns() - start
        metadata["timing"] = timing_summary(frames, acquisition_ns, startup_ns, total_timeouts)
        metadata["warmup_frames_discarded"] = 2 if args.raw8 else 1
        metadata["after_acquisition"] = camera_snapshot(camera)
    finally:
        try:
            if streaming:
                vx.VxStopStreaming(camera)
        finally:
            vx.VxClose(camera)
    header = f"P5\n{args.width} {args.height}\n255\n".encode()
    save_start = time.monotonic_ns()
    for index, frame in enumerate(frames):
        payload = frame.pop("payload")
        data = header + payload
        pixels = np.frombuffer(payload, np.uint8)
        frame["file"] = f"frame-{index:06d}.pgm"
        write_start = time.monotonic_ns()
        (output / frame["file"]).write_bytes(data)
        frame["write_duration_ms"] = (time.monotonic_ns() - write_start) / 1e6
        frame["file_bytes"] = len(data)
        frame["sha256"] = hashlib.sha256(data).hexdigest()
        frame["pixels"] = {"min": int(pixels.min()), "max": int(pixels.max()),
                           "mean": float(pixels.mean()), "std": float(pixels.std()),
                           "p1_p50_p99": np.percentile(pixels, [1, 50, 99]).tolist(),
                           "zero_fraction": float(np.mean(pixels == 0)),
                           "saturated_fraction": float(np.mean(pixels == 255))}
    metadata["timing"]["save_and_analysis_seconds"] = (time.monotonic_ns() - save_start) / 1e9
    metadata["frame_count"] = len(frames)
    metadata["distinct_frames"] = len({frame["sha256"] for frame in frames})
    metadata["frames"] = frames
    host_rate = metadata["timing"]["host_delivery_fps"]
    metadata["validation"] = {
        "fully_saturated_frames": sum(frame["pixels"]["saturated_fraction"] == 1.0 for frame in frames),
        "uniform_frames": sum(frame["pixels"]["min"] == frame["pixels"]["max"] for frame in frames),
        "duplicate_frames": len(frames) - metadata["distinct_frames"],
        "host_delivery_below_requested": host_rate < args.fps * 0.9 if host_rate is not None else None,
    }
    metadata["capture_completed_utc"] = utc_now()
    (output / "capture.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Saved {len(frames)} {args.width}x{args.height} {mode} frames "
          f"and capture.json to {output.resolve()}")
    host_fps = metadata["timing"]["host_delivery_fps"]
    rate = f"{host_fps:.2f} fps" if host_fps is not None else "unavailable (one frame)"
    print(f"Requested {args.fps} fps; measured host delivery {rate}; {total_timeouts} timeouts.")


if __name__ == "__main__":
    main()
