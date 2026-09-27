import cv2
import numpy as np
import pyvizionsdk
from pyvizionsdk import VX_IMAGE_FORMAT


def main():
    # Discover and open camera 0.
    count, devices = pyvizionsdk.VxDiscoverCameraDevices()
    if count <= 0 or not devices:
        print("No camera found.")
        return 1
    camera = pyvizionsdk.VxInitialCameraDevice(0)
    if camera is None or pyvizionsdk.VxOpen(camera) != 0:
        print("Could not open camera.")
        return 1

    streaming = False
    try:
        # Select the first UYVY format reported by the camera.
        result, formats = pyvizionsdk.VxGetFormatList(camera)
        if result != 0:
            raise RuntimeError("Could not read formats.")
        selected = None
        for candidate in formats:
            if candidate.format == VX_IMAGE_FORMAT.VX_IMAGE_FORMAT_UYVY:
                selected = candidate
                break
        if selected is None or not selected.width or not selected.height or selected.width % 2:
            raise RuntimeError("No usable UYVY format.")
        if pyvizionsdk.VxSetFormat(camera, selected) != 0:
            raise RuntimeError("Could not set format.")
        if pyvizionsdk.VxStartStreaming(camera) != 0:
            raise RuntimeError("Could not start streaming.")
        streaming = True

        print("Press q or Esc to quit.")
        while True:
            result, frame = pyvizionsdk.VxGetImage(camera, 2500, selected)
            if result != 0:
                raise RuntimeError(f"Frame capture failed: {result}")
            uyvy = np.frombuffer(frame, dtype=np.uint8).reshape(selected.height, selected.width, 2)
            # Mono sensor: Y is the image, U/V are constant.
            image = cv2.cvtColor(uyvy, cv2.COLOR_YUV2GRAY_UYVY)
            cv2.imshow("TEVS AR0822", image)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
    finally:
        if streaming:
            pyvizionsdk.VxStopStreaming(camera)
        pyvizionsdk.VxClose(camera)
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
    except (RuntimeError, ValueError, cv2.error) as error:
        print(error)
        raise SystemExit(1)
