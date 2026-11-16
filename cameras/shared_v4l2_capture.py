"""BOTH cameras: V4L2 capture from memory-mapped driver buffers. Plain V4L2, nothing camera specific.

Cut down from Vision Components' example capture_y10.py (GPL-2.0):
https://github.com/VC-MIPI-modules/vc_mipi_raspi/blob/df65f88/examples/capture_y10.py
Ours: the buffer offset is read at byte 64 (VC's 68 maps every buffer at 0); dequeue_buffer returns the capture time.
"""
import fcntl
import mmap
import os
import struct


def _IOC(direction, type, nr, size):  # An ioctl request code. direction: 1 = we write, 3 = we write and the driver answers
    return direction << 30 | size << 16 | ord(type) << 8 | nr


def _IOW(type, nr, size):
    return _IOC(1, type, nr, size)


def _IOWR(type, nr, size):
    return _IOC(3, type, nr, size)


VIDIOC_S_FMT = _IOWR("V", 5, 208)  # 'V' = video, the command number, the size of the struct sent
VIDIOC_REQBUFS = _IOWR("V", 8, 20)
VIDIOC_QUERYBUF = _IOWR("V", 9, 88)
VIDIOC_QBUF = _IOWR("V", 15, 88)
VIDIOC_DQBUF = _IOWR("V", 17, 88)
VIDIOC_STREAMON = _IOW("V", 18, 4)
VIDIOC_STREAMOFF = _IOW("V", 19, 4)
CAPTURE, MMAP, FIELD_NONE = 1, 1, 1  # V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_MEMORY_MMAP, V4L2_FIELD_NONE

# Byte offsets of the fields we use, from linux/videodev2.h (checked with offsetof on the Jetson). Every field is a
# 4-byte unsigned int ("I") unless noted.
# struct v4l2_format, 208 bytes. The union after type holds pointers, so it starts at 8: bytes 4-7 are padding.
FMT_TYPE = 0
FMT_WIDTH = 8           # fmt.pix.width
FMT_HEIGHT = 12         # fmt.pix.height
FMT_PIXELFORMAT = 16    # fmt.pix.pixelformat
FMT_FIELD = 20          # fmt.pix.field
# struct v4l2_requestbuffers, 20 bytes (the last 8 are reserved)
REQ_COUNT = 0
REQ_TYPE = 4
REQ_MEMORY = 8
# struct v4l2_buffer, 88 bytes. timestamp holds 64-bit numbers, so it starts at 24: bytes 20-23 are padding.
BUF_INDEX = 0
BUF_TYPE = 4
BUF_SECONDS = 24        # timestamp.tv_sec, 8-byte signed ("q")
BUF_MICROSECONDS = 32   # timestamp.tv_usec, 8-byte signed ("q")
BUF_MEMORY = 60
BUF_OFFSET = 64         # m.offset: first 4 bytes of an 8-byte union
BUF_LENGTH = 72


class V4L2Capture:
    def __init__(self, device):
        self.device, self.buffers = device, []  # buffers: the driver's buffers, mapped into our memory

    def open(self):
        self.fd = os.open(self.device, os.O_RDWR)

    def set_format(self, width, height, pixelformat):
        fmt = bytearray(208)
        struct.pack_into("I", fmt, FMT_TYPE, CAPTURE)
        struct.pack_into("I", fmt, FMT_WIDTH, width)
        struct.pack_into("I", fmt, FMT_HEIGHT, height)
        struct.pack_into("I", fmt, FMT_PIXELFORMAT, pixelformat)
        struct.pack_into("I", fmt, FMT_FIELD, FIELD_NONE)
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
        # What the driver actually set
        self.width = struct.unpack_from("I", fmt, FMT_WIDTH)[0]
        self.height = struct.unpack_from("I", fmt, FMT_HEIGHT)[0]
        self.pixelformat = struct.unpack_from("I", fmt, FMT_PIXELFORMAT)[0]

    def _buffer(self, index=0):  # A struct v4l2_buffer naming buffer index of our capture stream
        buf = bytearray(88)
        struct.pack_into("I", buf, BUF_INDEX, index)
        struct.pack_into("I", buf, BUF_TYPE, CAPTURE)
        struct.pack_into("I", buf, BUF_MEMORY, MMAP)
        return buf

    def request_buffers(self, count=4):
        req = bytearray(20)
        struct.pack_into("I", req, REQ_COUNT, count)
        struct.pack_into("I", req, REQ_TYPE, CAPTURE)
        struct.pack_into("I", req, REQ_MEMORY, MMAP)
        fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)
        given = struct.unpack_from("I", req, REQ_COUNT)[0]  # The driver may give a different number
        for index in range(given):
            buf = self._buffer(index)
            fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, buf)
            offset = struct.unpack_from("I", buf, BUF_OFFSET)[0]  # Which buffer, as the driver names it to mmap
            length = struct.unpack_from("I", buf, BUF_LENGTH)[0]
            self.buffers.append(mmap.mmap(self.fd, length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=offset))

    def queue_buffer(self, index):  # Give a buffer to the driver to fill
        fcntl.ioctl(self.fd, VIDIOC_QBUF, self._buffer(index))

    def dequeue_buffer(self):  # Wait for the next filled buffer: (its index, the driver's capture time in seconds)
        buf = self._buffer()  # The driver writes the index in
        fcntl.ioctl(self.fd, VIDIOC_DQBUF, buf)
        index = struct.unpack_from("I", buf, BUF_INDEX)[0]
        seconds = struct.unpack_from("q", buf, BUF_SECONDS)[0]
        microseconds = struct.unpack_from("q", buf, BUF_MICROSECONDS)[0]
        return index, seconds + microseconds / 1e6

    def stream_on(self):
        fcntl.ioctl(self.fd, VIDIOC_STREAMON, struct.pack("I", CAPTURE))  # The ioctl takes just the buffer type

    def stream_off(self):
        fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, struct.pack("I", CAPTURE))

    def close(self):
        try:
            self.stream_off()
        finally:
            for mem in self.buffers:
                mem.close()
            os.close(self.fd)
