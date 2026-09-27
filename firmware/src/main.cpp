#include "VizionSDK.h"
#include <opencv2/highgui.hpp>
#include <opencv2/imgproc.hpp>
#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

int main() {
    // Discover and open camera 0.
    std::vector<std::string> devices;
    if (VxDiscoverCameraDevices(devices) <= 0) return 1;
    auto camera = VxInitialCameraDevice(0);
    if (!camera || VxOpen(camera) != 0) return 1;

    // Select the first UYVY format reported by the camera.
    std::vector<VxFormat> formats;
    VxFormat format{};
    if (VxGetFormatList(camera, formats) == 0) {
        for (const auto& candidate : formats) {
            if (candidate.format == VX_IMAGE_FORMAT::UYVY) {
                format = candidate;
                break;
            }
        }
    }
    if (!format.width || !format.height || format.width % 2 ||
        VxSetFormat(camera, format) != 0) {
        std::cerr << "Could not select a UYVY format.\n";
        VxClose(camera);
        return 1;
    }

    // UYVY uses two bytes per pixel.
    std::vector<uint8_t> buffer(std::size_t(format.width) * format.height * 2);
    if (VxStartStreaming(camera) != 0) {
        VxClose(camera);
        return 1;
    }

    int status = 0;
    try {
        std::cout << "Press q or Esc to quit.\n";
        while (true) {
            int size = 0;
            auto result = VxGetImage(camera, buffer.data(), &size, 2500);
            if (result != VX_CAPTURE_RESULT::VX_SUCCESS ||
                size <= 0 || std::size_t(size) != buffer.size()) {
                std::cerr << "Frame capture failed.\n";
                status = 1;
                break;
            }
            cv::Mat uyvy(format.height, format.width, CV_8UC2, buffer.data());
            // Mono sensor: Y is the image, U/V are constant.
            cv::Mat image;
            cv::cvtColor(uyvy, image, cv::COLOR_YUV2GRAY_UYVY);
            cv::imshow("TEVS AR0822", image);
            int key = cv::waitKey(1);
            if (key == 'q' || key == 27) break;
        }
    } catch (const cv::Exception& error) {
        std::cerr << error.what() << '\n';
        status = 1;
    }

    if (VxStopStreaming(camera) != 0) status = 1;
    if (VxClose(camera) != 0) status = 1;
    cv::destroyAllWindows();
    return status;
}
