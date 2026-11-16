"""Camera capture. The file name says which camera the code is for:

    imx412.py               IMX412C only: the colour camera ("color")
    tevs.py                 TEVS only: the mono fluorescence camera ("fl")
    shared.py               both cameras
    shared_v4l2_capture.py  both cameras: the V4L2 calls

A name ending in _pi or _jetson runs only on that computer. dual_stream.py, camera_ui.py and recording.py are for
both cameras and reach the two camera files through CAMERAS[name]; those files define the same names for that.
"""
from . import imx412, shared, tevs  # noqa: F401

CAMERAS = {tevs.NAME: tevs, imx412.NAME: imx412}  # "fl", "color": the order the UI shows them
