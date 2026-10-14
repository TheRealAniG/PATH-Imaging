import numpy as np
import camera_API as vx

vx.VxDiscoverCameraDevices()
cam = vx.VxInitialCameraDevice(0)
vx.VxOpen(cam)
_, fl = vx.VxGetFormatList(cam)
fmt = next(f for f in fl if f.format == vx.VX_IMAGE_FORMAT_RAW10 and (f.width, f.height, f.framerate) == (1280, 720, 30))
vx.VxSetFormat(cam, fmt)
vx.VxStartStreaming(cam)
_, img = vx.VxGetImage(cam, 2500, fmt)
vx.VxStopStreaming(cam)
vx.VxClose(cam)
print("range", img.min(), img.max(), "| low-2-bit counts (00,01,10,11):", np.bincount(img & 3, minlength=4))
