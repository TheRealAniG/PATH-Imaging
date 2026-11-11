"""Lossless RAW10 video: recorded on the Jetson's GPU by the Record button in camera_ui.py, read back here.

Every frame is compressed on its own with JPEG 2000 in its lossless mode (High-Throughput variant, NVIDIA nvJPEG2000),
so what comes back is bit for bit the 10-bit values the camera delivered. This is not JPEG: nothing is rounded and
nothing is reduced to 8 bits. One file per camera, <name>.j2ks:

    line 1   "PATH RAW10 recording 1"
    line 2   JSON: camera, sensor, width, height, pattern, frame rate, exposure, gain, start time, ...
    then, for each frame:  8 bytes  the driver's capture time (float64, seconds, monotonic clock)
                           4 bytes  length of the picture that follows (uint32)
                           a JPEG 2000 codestream. Mono: one 10-bit plane. Colour: the four Bayer planes
                           (R, G on the red row, G on the blue row, B), each half the width and height.

Measured on the Jetson Orin Nano with both cameras live: 1920x1080 at 60 fps takes 10 ms a frame and 4032x3040 at
20 fps about 40 ms, at 12-37 % and 33 % of the 16-bit size (about 12 GB a minute for both). A minute in MAXN_SUPER
power mode lost no frame; in 25W mode, with other programs busy, the colour camera lost 2-5 in a hundred.
Recording needs the GPU; reading does not (OpenCV decodes the frames, more slowly).

Read a recording:
    from recording import Recording
    rec = Recording("file.j2ks")     # rec.info, len(rec), rec.times; rec[i] is a (height, width) uint16 array of 0-1023
From the shell:
    python recording.py file.j2ks                what is in it, and how many frames are missing (gaps in the capture times)
    python recording.py file.j2ks --check        also decodes every frame
    python recording.py file.j2ks --png 0        frame 0 as a 16-bit PNG beside it (colour demosaiced, not white balanced)
    python recording.py file.j2ks --mp4          a viewing copy beside it: the picture the live preview shows, as H.264.
                                                 Lossy and 8-bit; the recording is not changed.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import struct
import sys
import threading
import time

import numpy as np

SUFFIX = ".j2ks"
MAGIC = b"PATH RAW10 recording 1\n"
FRAME = struct.Struct("<dI")  # Capture time, length
PLANES = ((0, 0), (0, 1), (1, 0), (1, 1))  # Row and column offset of each Bayer plane in the mosaic
WAITING_BYTES = 60e6  # Frames waiting for the encoder may hold this much memory (at least 3 frames); beyond it frames are dropped


def _nvidia():
    """NVIDIA's image codec module. Its first import prints errors about formats we do not use (TIFF, JPEG), so that
    one import runs with the error output switched off; a real failure still raises ImportError."""
    if "nvidia.nvimgcodec" not in sys.modules:
        sys.stderr.flush()
        keep, null = os.dup(2), os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 2)
        try:
            import nvidia.nvimgcodec  # noqa: F401
        finally:
            os.dup2(keep, 2)
            os.close(keep)
            os.close(null)
    return sys.modules["nvidia.nvimgcodec"]


def unavailable():
    """None if this machine can record, otherwise the reason as text."""
    try:
        encoders = _nvidia().get_encoder_ids()
    except ImportError:
        return "recording needs the nvidia-nvimgcodec-cu13 and nvidia-nvjpeg2k-cu13 packages (Jetson only)"
    return None if "nvjpeg2k_encoder" in encoders else "no GPU JPEG 2000 encoder on this machine"


class Recorder:
    def __init__(self, path, name, height, width, info):
        """Start recording (height, width) frames of camera name ('fl' is mono, 'color' is Bayer RGGB) to path.
        info: a dict of anything worth keeping with the file. Returns once the GPU encoder is ready."""
        self.path, self.mono = Path(path), name == "fl"
        self.written = self.dropped = self.bytes = 0
        self.error = None
        self.started = time.time()
        self.times = []  # Capture time of every frame offered, to count the ones the camera skipped
        self.buf = np.empty((height, width, 1) if self.mono else (4, height // 2, width // 2), np.uint16)
        self.waiting = queue.Queue(max(3, int(WAITING_BYTES // (width * height * 2))))
        self.pool = ThreadPoolExecutor(4)  # Splits a colour frame into its planes: 6 ms on four threads, 13 ms on one
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "wb", buffering=0)
        self.file.write(MAGIC + json.dumps({
            **info, "camera": name, "width": width, "height": height, "pattern": "mono" if self.mono else "RGGB", "bits": 10,
            "codec": "JPEG 2000 High-Throughput, lossless", "planes": 1 if self.mono else 4,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started))}).encode() + b"\n")
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._encode)
        self.thread.start()
        self.ready.wait()

    def add(self, frame, stamp):
        """One frame as the capture loop has it (10-bit value << 6) with the driver's capture time. Never waits."""
        self.times.append(stamp)
        try:
            self.waiting.put_nowait((stamp, frame))
        except queue.Full:
            self.dropped += 1

    def _image(self, n, frame):  # True 10-bit values in the layout the encoder takes, in one pass over the frame
        if self.mono:
            np.right_shift(frame, 6, out=self.buf[:, :, 0])
            return n.as_image(self.buf, precision=10)
        list(self.pool.map(lambda plane: np.right_shift(frame[PLANES[plane][0]::2, PLANES[plane][1]::2], 6, out=self.buf[plane]), range(4)))
        return n.as_image(self.buf, sample_format=n.SampleFormat.P_UNCHANGED, precision=10)

    def _encode(self):
        n = _nvidia()
        try:
            encoder = n.Encoder(backends=[n.Backend(n.BackendKind.GPU_ONLY)])
            params = n.EncodeParams(
                quality_type=n.QualityType.LOSSLESS, color_spec=n.ColorSpec.GRAY if self.mono else n.ColorSpec.UNCHANGED,
                jpeg2k_encode_params=n.Jpeg2kEncodeParams(ht=True, bitstream_type=n.Jpeg2kBitstreamType.J2K))
            shape = self.buf.shape[:2] if self.mono else (self.buf.shape[1] * 2, self.buf.shape[2] * 2)
            noise = np.random.default_rng(0).integers(0, 1024, shape, np.uint16) << 6  # As hard to compress as a frame gets
            encoder.encode(self._image(n, noise), "jpeg2k", params=params)  # The first one is slow: do it before recording
        except Exception as error:
            self.error = f"GPU encoder did not start: {error}"
        self.ready.set()
        while (item := self.waiting.get()) is not None:
            if self.error:
                continue  # Keep emptying the queue so close() returns
            stamp, frame = item
            try:
                data = memoryview(encoder.encode(self._image(n, frame), "jpeg2k", params=params))
                self.file.write(FRAME.pack(stamp, data.nbytes))
                self.file.write(data)
                self.written, self.bytes = self.written + 1, self.bytes + data.nbytes
            except Exception as error:
                self.error = f"stopped after {self.written} frames: {error}"

    @property
    def skipped(self):  # Frames the camera did not deliver while recording
        return missing(self.times)[0]

    def close(self):  # Saves the frames still waiting, then closes the file
        self.waiting.put(None)
        self.thread.join()
        self.pool.shutdown()
        self.file.close()


def missing(times):
    """(frames missing, frames per second) from capture times: a camera's frames are evenly spaced, so a gap of two
    frame times means the driver had no free buffer for one frame."""
    if len(times) < 3:
        return 0, 0.0
    gaps = np.diff(times)
    interval = float(np.median(gaps))
    return int(np.maximum(np.rint(gaps / interval) - 1, 0).sum()), 1 / interval


def _decoder():
    """A function turning one stored picture into its planes, shape (planes, height, width). NVIDIA's decoder when it is
    installed, otherwise OpenCV, which is all another computer needs."""
    try:
        n = _nvidia()
        decoder = n.Decoder()
        params = n.DecodeParams(color_spec=n.ColorSpec.UNCHANGED, allow_any_depth=True)
        return lambda data: np.asarray(decoder.decode(data, params=params).cpu()).transpose(2, 0, 1)
    except ImportError:
        import cv2
        cv2.setLogLevel(2)  # Errors only: it warns on every frame that the pictures name no colour space

        def planes(data):
            image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
            return image[None] if image.ndim == 2 else image[:, :, [2, 1, 0, 3]].transpose(2, 0, 1)  # OpenCV swaps planes 0 and 2
        return planes


class Recording:
    def __init__(self, path):
        self.path = Path(path)
        self.file = open(self.path, "rb")
        if self.file.readline() != MAGIC:
            raise ValueError(f"{path} is not a PATH RAW10 recording")
        self.info = json.loads(self.file.readline())
        self.times, self.where = [], []
        end = self.path.stat().st_size
        while len(head := self.file.read(FRAME.size)) == FRAME.size:
            stamp, length = FRAME.unpack(head)
            if self.file.tell() + length > end:
                break  # The last frame was cut off (power loss, full disk): leave it out
            self.times.append(stamp)
            self.where.append((self.file.tell(), length))
            self.file.seek(length, 1)
        self.times = np.array(self.times)
        self._decode = None

    def __len__(self):
        return len(self.where)

    def __getitem__(self, i):
        """Frame i as a (height, width) uint16 array of the 10-bit values, 0-1023."""
        self._decode = self._decode or _decoder()
        offset, length = self.where[range(len(self))[i]]
        self.file.seek(offset)
        planes = self._decode(self.file.read(length))
        if len(planes) == 1:
            return planes[0]
        frame = np.empty((self.info["height"], self.info["width"]), np.uint16)
        for plane, (row, column) in zip(planes, PLANES):
            frame[row::2, column::2] = plane
        return frame

    def skipped(self):
        """(frames the camera skipped, frames per second it ran at), from the capture times."""
        return missing(self.times)

    def close(self):
        self._decode = None
        self.file.close()


def to_mp4(rec, out):
    """A viewing copy: each frame as the live preview shows it (stretched, colour white balanced), H.264 in an .mp4."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    from dual_stream import preview
    Gst.init(None)
    name, fps = rec.info["camera"], max(1, round(rec.skipped()[1]))
    pipe = None
    for i in range(len(rec)):
        shown = preview(name, rec[i] << 6, max(1, rec.info["width"] // (1280 if name == "fl" else 2000)))  # Colour halves again: 4032 wide becomes 1008
        shown = (shown >> 8).astype(np.uint8) if name == "fl" else shown
        shown = np.ascontiguousarray(shown[:shown.shape[0] // 2 * 2, :shown.shape[1] // 2 * 2])  # H.264 wants even sizes
        if pipe is None:
            pipe = Gst.parse_launch(
                f"appsrc name=src format=time caps=video/x-raw,format={'GRAY8' if name == 'fl' else 'BGR'},width={shown.shape[1]},"
                f"height={shown.shape[0]},framerate={fps}/1 ! videoconvert ! video/x-raw,format=I420 "  # Left alone, colour becomes 4:4:4, which players refuse
                f"! x264enc speed-preset=veryfast bitrate=6000 ! h264parse "
                f"! mp4mux ! filesink location={out}")
            pipe.set_state(Gst.State.PLAYING)
        buffer = Gst.Buffer.new_wrapped(shown.tobytes())
        buffer.pts = buffer.dts = i * Gst.SECOND // fps
        buffer.duration = Gst.SECOND // fps
        pipe.get_by_name("src").emit("push-buffer", buffer)
        print(f"\r{i + 1} of {len(rec)} frames", end="", flush=True)
    pipe.get_by_name("src").emit("end-of-stream")
    pipe.get_bus().timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    print(f"\rsaved {out} ({fps} fps, {shown.shape[1]}x{shown.shape[0]})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    rec = Recording(sys.argv[1])
    print(*(f"{key}: {value}" for key, value in rec.info.items()), sep="\n")
    gone, fps = rec.skipped()
    raw = len(rec) * rec.info["width"] * rec.info["height"] * 2
    seconds = rec.times[-1] - rec.times[0] if len(rec) > 1 else 0
    print(f"{len(rec)} frames over {seconds:.1f} s at {fps:.2f} frames/s; {gone} missing; "
          f"{rec.path.stat().st_size / 1e6:.0f} MB, {100 * rec.path.stat().st_size / max(raw, 1):.1f} % of the {raw / 1e6:.0f} MB as 16-bit raw")
    if "--check" in sys.argv:
        for i in range(len(rec)):
            frame = rec[i]
            print(f"\rdecoded {i + 1} of {len(rec)}", end="", flush=True)
        print(f"\rall {len(rec)} frames decode; the last one spans {frame.min()}..{frame.max()}")
    if "--png" in sys.argv:
        import cv2
        i = int(sys.argv[sys.argv.index("--png") + 1])
        out = f"{rec.path.with_suffix('')}_frame{i}.png"
        cv2.imwrite(out, rec[i] << 6 if rec.info["pattern"] == "mono" else cv2.cvtColor(rec[i] << 6, cv2.COLOR_BayerBG2BGR))
        print(f"saved {out}")
    if "--mp4" in sys.argv:
        to_mp4(rec, f"{rec.path.with_suffix('')}.mp4")
    rec.close()
