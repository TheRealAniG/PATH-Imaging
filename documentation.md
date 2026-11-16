# Camera capture

![V4L2 capture flow](image.png)

Capturing from the camera is not simple. It follows the sequence shown above.

## Functions

These are the functions we have for it:

- `set_format()`
- `request_buffers()`
- `queue_buffer()`
- `dequeue_buffer()`
- `stream_on()`
- `stream_off()`

All of them work the same way underneath: they use the **V4L2 API** (Video4Linux2). V4L2 is the Linux kernel's interface for video devices such as cameras. The camera shows up as a device file (`/dev/videoN`), and we control it by sending commands to that file with **ioctl** (I/O control, a Linux system call).

## How ioctl works

We'll use `set_format()` as the example:

```python
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
```

It writes each setting into a byte array and then sends that array with ioctl. `"I"` is one 4-byte number. A name like `FMT_WIDTH` is where that setting sits in the array (byte 8). The functions and these names are in `cameras/shared_v4l2_capture.py`.

### Decoding the call

```python
fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
```

| Argument | Meaning |
|---|---|
| `self.fd` | File descriptor of the open video device |
| `VIDIOC_S_FMT` | The request code for the "set format" command |
| `fmt` | The byte array holding all the settings we send |

### Decoding `VIDIOC_S_FMT`

```python
VIDIOC_S_FMT = _IOWR("V", 5, 208)
```

`_IOWR(type, nr, size)` builds the request code:

1. **`_IOWR`** means the data goes both ways: we write it in and the kernel writes it back. Similarly, `_IOR` is read-only and `_IOW` is write-only.
2. **`type`**: `'V'` is the letter shared by all V4L2 commands.
3. **`nr`**: the command number, which tells V4L2 commands apart. `5` means "set format".
4. **`size`**: the size of the data sent, here `struct v4l2_format`, which is 208 bytes.

The four fields are packed into a single 32-bit number:

| Field | Value | Shifted into place |
|---|---|---|
| direction (read + write) | `3` | `3 << 30` = `0xC0000000` |
| size | `208` | `208 << 16` = `0x00D00000` |
| type | `'V'` = `0x56` | `0x56 << 8` = `0x00005600` |
| nr | `5` | `0x00000005` |
| **`VIDIOC_S_FMT`** | | **`0xC0D05605`** |

You don't need to compute this yourself. It's just a constant defined by V4L2's rules, the same value the kernel's C headers use.

### After the call

`fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)` sends the request to the Linux kernel. Because the call goes both ways, the kernel writes the values it actually used back into `fmt`, so we read them back to check them. Every command in the list above is defined this way.
