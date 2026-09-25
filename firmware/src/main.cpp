#include <iostream>
#include <vector>
#include <string>
#include <memory>

#include "VizionSDK.h"

#include <opencv2/core.hpp>
#include <opencv2/highgui.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/imgcodecs.hpp>

int main()
{
    // ---------------------------------------------------------
    // 1. Find TechNexion cameras
    // ---------------------------------------------------------
    std::vector<std::string> deviceList;

    int cameraCount = VxDiscoverCameraDevices(deviceList);

    if (cameraCount <= 0)
    {
        std::cerr << "No TechNexion camera detected." << std::endl;
        return 1;
    }

    std::cout << "Found " << cameraCount << " camera(s):" << std::endl;

    for (size_t i = 0; i < deviceList.size(); ++i)
    {
        std::cout << "[" << i << "] "
                  << deviceList[i] << std::endl;
    }


    // ---------------------------------------------------------
    // 2. Create camera object
    //
    // We're just using camera 0.
    // ---------------------------------------------------------
    std::shared_ptr<VxCamera> camera =
        VxInitialCameraDevice(0);

    if (!camera)
    {
        std::cerr << "Could not initialize camera." << std::endl;
        return 1;
    }


    // ---------------------------------------------------------
    // 3. Open camera
    // ---------------------------------------------------------
    if (VxOpen(camera) != 0)
    {
        std::cerr << "Could not open camera." << std::endl;
        return 1;
    }

    std::cout << "Camera opened successfully." << std::endl;


    // ---------------------------------------------------------
    // 4. Print camera information
    // ---------------------------------------------------------
    std::string cameraName;

    if (VxGetDeviceName(camera, cameraName) == 0)
    {
        std::cout << "Camera: "
                  << cameraName << std::endl;
    }


    // ---------------------------------------------------------
    // 5. Ask camera which resolutions/formats it supports
    // ---------------------------------------------------------
    std::vector<VxFormat> formatList;

    if (VxGetFormatList(camera, formatList) != 0 ||
        formatList.empty())
    {
        std::cerr << "Could not obtain camera formats."
                  << std::endl;

        VxClose(camera);
        return 1;
    }

    std::cout << "\nSupported formats:\n";

    for (size_t i = 0; i < formatList.size(); ++i)
    {
        std::cout
            << "[" << i << "] "
            << formatList[i].width << "x"
            << formatList[i].height
            << " @ "
            << formatList[i].framerate
            << " fps, format="
            << static_cast<int>(formatList[i].format)
            << std::endl;
    }


    // ---------------------------------------------------------
    // 6. Prefer UYVY.
    //
    // TEVS MIPI cameras commonly expose UYVY.
    // If UYVY isn't available, try MJPG.
    //
    // IMPORTANT:
    // We don't invent a resolution.
    // We choose one reported by VxGetFormatList().
    // ---------------------------------------------------------
    VxFormat selectedFormat{};
    bool foundFormat = false;

    for (const VxFormat& format : formatList)
    {
        if (format.format == VX_IMAGE_FORMAT::UYVY)
        {
            selectedFormat = format;
            foundFormat = true;
            break;
        }
    }

    if (!foundFormat)
    {
        for (const VxFormat& format : formatList)
        {
            if (format.format == VX_IMAGE_FORMAT::MJPG)
            {
                selectedFormat = format;
                foundFormat = true;
                break;
            }
        }
    }

    if (!foundFormat)
    {
        std::cerr
            << "Could not find UYVY or MJPG format."
            << std::endl;

        VxClose(camera);
        return 1;
    }


    std::cout
        << "\nUsing "
        << selectedFormat.width << "x"
        << selectedFormat.height
        << " @ "
        << selectedFormat.framerate
        << " fps"
        << std::endl;


    // ---------------------------------------------------------
    // 7. Tell camera to use that format
    // ---------------------------------------------------------
    if (VxSetFormat(camera, selectedFormat) != 0)
    {
        std::cerr << "Could not set camera format."
                  << std::endl;

        VxClose(camera);
        return 1;
    }


    // ---------------------------------------------------------
    // 8. Start the CSI camera stream
    // ---------------------------------------------------------
    if (VxStartStreaming(camera) != 0)
    {
        std::cerr << "Could not start camera stream."
                  << std::endl;

        VxClose(camera);
        return 1;
    }

    std::cout << "Streaming started." << std::endl;
    std::cout << "Press q or ESC to quit." << std::endl;


    // ---------------------------------------------------------
    // 9. Allocate image buffer
    //
    // UYVY is 16 bits / pixel = 2 bytes / pixel.
    //
    // Give ourselves extra room so the same buffer can also
    // hold compressed MJPG data.
    // ---------------------------------------------------------
    const size_t bufferSize =
        static_cast<size_t>(selectedFormat.width) *
        static_cast<size_t>(selectedFormat.height) *
        4;

    std::vector<uint8_t> imageBuffer(bufferSize);


    // ---------------------------------------------------------
    // 10. Camera capture loop
    // ---------------------------------------------------------
    while (true)
    {
        int bytesReceived = 0;

        VX_CAPTURE_RESULT result =
            VxGetImage(
                camera,
                imageBuffer.data(),
                &bytesReceived,
                2500
            );

        if (result != VX_CAPTURE_RESULT::VX_SUCCESS)
        {
            std::cerr
                << "Frame capture failed: "
                << static_cast<int>(result)
                << std::endl;

            continue;
        }


        // -----------------------------------------------------
        // UYVY camera image
        // -----------------------------------------------------
        if (selectedFormat.format ==
            VX_IMAGE_FORMAT::UYVY)
        {
            cv::Mat uyvy(
                selectedFormat.height,
                selectedFormat.width,
                CV_8UC2,
                imageBuffer.data()
            );

            cv::Mat bgr;

            cv::cvtColor(
                uyvy,
                bgr,
                cv::COLOR_YUV2BGR_UYVY
            );

            cv::imshow(
                "TEVS AR0822",
                bgr
            );
        }


        // -----------------------------------------------------
        // MJPEG camera image
        // -----------------------------------------------------
        else if (selectedFormat.format ==
                 VX_IMAGE_FORMAT::MJPG)
        {
            cv::Mat compressed(
                1,
                bytesReceived,
                CV_8UC1,
                imageBuffer.data()
            );

            cv::Mat image =
                cv::imdecode(
                    compressed,
                    cv::IMREAD_COLOR
                );

            if (!image.empty())
            {
                cv::imshow(
                    "TEVS AR0822",
                    image
                );
            }
        }


        // -----------------------------------------------------
        // GUI event handling
        //
        // waitKey() also allows OpenCV to refresh the window.
        // -----------------------------------------------------
        int key = cv::waitKey(1);

        if (key == 'q' || key == 27)
        {
            break;
        }
    }


    // ---------------------------------------------------------
    // 11. Clean shutdown
    // ---------------------------------------------------------
    VxStopStreaming(camera);
    VxClose(camera);

    cv::destroyAllWindows();

    std::cout << "Camera closed." << std::endl;

    return 0;
}