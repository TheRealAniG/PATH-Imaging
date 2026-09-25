#include "VizionSDK.h"
#include <opencv2/core.hpp>
#include <opencv2/highgui.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <algorithm>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

int main() {
    // Discover and open the first camera.
    std::vector<std::string> devices;
    if (VxDiscoverCameraDevices(devices) <= 0 || devices.empty()) {
        std::cerr << "No camera found.\n";
        return 1;
    }
    std::cout << "Opening " << devices.front() << '\n';
    auto camera = VxInitialCameraDevice(0);
    if (!camera || VxOpen(camera) != 0) {
        std::cerr << "Could not open camera.\n";
        return 1;
    }

    bool streaming = false;
    int status = 0;
    try {
        // Choose a reported format: prefer UYVY, then MJPG.
        std::vector<VxFormat> formats;
        if (VxGetFormatList(camera, formats) != 0)
            throw std::runtime_error("Could not read camera formats.");
        auto selected = std::find_if(formats.begin(), formats.end(), [](const VxFormat& f) {
            return f.format == VX_IMAGE_FORMAT::UYVY;
        });
        if (selected == formats.end())
            selected = std::find_if(formats.begin(), formats.end(), [](const VxFormat& f) {
                return f.format == VX_IMAGE_FORMAT::MJPG;
            });
        if (selected == formats.end() || !selected->width || !selected->height)
            throw std::runtime_error("No usable UYVY or MJPG format.");
        const auto format = *selected;
        if (format.format == VX_IMAGE_FORMAT::UYVY && format.width % 2)
            throw std::runtime_error("UYVY requires even width.");
        if (VxSetFormat(camera, format) != 0)
            throw std::runtime_error("Could not set camera format.");

        // SDK supplies the byte count; it does not accept buffer capacity.
        std::vector<uint8_t> buffer(std::size_t(format.width) * format.height * 4);
        if (VxStartStreaming(camera) != 0)
            throw std::runtime_error("Could not start streaming.");
        streaming = true;
        cv::namedWindow("TEVS AR0822", cv::WINDOW_NORMAL);
        std::cout << format.width << 'x' << format.height << " @ "
                  << format.framerate << " fps. Press q or Esc to quit.\n";

        // Retrieve frames and display the live preview.
        while (true) {
            int bytes = 0;
            const auto result = VxGetImage(camera, buffer.data(), &bytes, 2500);
            if (result == VX_CAPTURE_RESULT::VX_SUCCESS) {
                if (bytes <= 0 || std::size_t(bytes) > buffer.size())
                    throw std::runtime_error("Invalid frame size.");
                cv::Mat image;
                if (format.format == VX_IMAGE_FORMAT::UYVY) {
                    if (std::size_t(bytes) != std::size_t(format.width) * format.height * 2)
                        throw std::runtime_error("Unexpected UYVY frame size.");
                    cv::Mat raw(format.height, format.width, CV_8UC2, buffer.data());
                    cv::cvtColor(raw, image, cv::COLOR_YUV2BGR_UYVY);
                } else {
                    image = cv::imdecode(cv::Mat(1, bytes, CV_8UC1, buffer.data()), cv::IMREAD_COLOR);
                }
                if (!image.empty()) cv::imshow("TEVS AR0822", image);
            } else if (result != VX_CAPTURE_RESULT::VX_TIMEOUT &&
                       result != VX_CAPTURE_RESULT::VX_BUFFER_CORRUPTED) {
                throw std::runtime_error("Capture failed: " + std::to_string(static_cast<int>(result)));
            }
            // Process GUI events even when a frame times out.
            const int key = cv::waitKey(1);
            if (key == 'q' || key == 27 || cv::getWindowProperty("TEVS AR0822", cv::WND_PROP_VISIBLE) < 1)
                break;
        }
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        status = 1;
    }

    if (streaming && VxStopStreaming(camera) != 0) status = 1;
    if (VxClose(camera) != 0) status = 1;
    cv::destroyAllWindows();
    return status;
}
