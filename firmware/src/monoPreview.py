import sys

import cv2


def main():
    # Run firmware/driver/setup_y8.sh first; pass the video node it prints.
    device = sys.argv[1] if len(sys.argv) > 1 else "/dev/video0"
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 1920
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 1080

    camera = cv2.VideoCapture(device, cv2.CAP_V4L2)
    camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"GREY"))
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if not camera.isOpened():
        print(f"Could not open {device}.")
        return 1

    try:
        print("Press q or Esc to quit.")
        while True:
            ok, frame = camera.read()
            if not ok:
                print("Frame capture failed.")
                return 1
            # OpenCV expands GREY to three equal channels; keep one.
            mono = frame[:, :, 0] if frame.ndim == 3 else frame
            cv2.imshow("TEVS AR0822 Y8", mono)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
