#pragma once
// Test double only. Never linked into the production target.
#include <algorithm>
#include <cstdint>
#include <csignal>
#include <cstdlib>
#include <fstream>
#include <memory>
#include <string>
#include <vector>
enum class VX_IMAGE_FORMAT { NONE, YUY2, UYVY, NV12, MJPG, BGRA, BGRX, BGR, RGB16, RGB };
enum class VX_CAMERA_INTERFACE_TYPE { INTERFACE_USB, INTERFACE_MIPI_CSI2, INTERFACE_ETHERNET };
enum class VX_CAPTURE_RESULT { VX_SUCCESS = 0, VX_TIMEOUT = -1, VX_CAM_OCCUPIED = -2, VX_OTHER_ERROR = -3, VX_BUFFER_CORRUPTED = -4 };
struct VxFormat { uint8_t mediatypeIdx; uint16_t width, height, framerate; VX_IMAGE_FORMAT format; };
struct VxCamera {};
inline bool scenario(const char* name) {
    const char* value = std::getenv("FAKE_SCENARIO");
    return value && std::string(value) == name;
}
inline int trace(const char* operation) {
    if (const char* path = std::getenv("FAKE_TRACE")) std::ofstream(path, std::ios::app) << operation << '\n';
    return scenario(operation) ? -1 : 0;
}
inline int VxDiscoverCameraDevices(std::vector<std::string>& devices) {
    if (scenario("no_devices")) return 0;
    devices = {"TEVS-AR0822"}; return 1;
}
inline std::shared_ptr<VxCamera> VxInitialCameraDevice(int) {
    return scenario("null_camera") ? nullptr : std::make_shared<VxCamera>();
}
inline int VxOpen(std::shared_ptr<VxCamera>) { return trace("open"); }
inline int VxClose(std::shared_ptr<VxCamera>) { return trace("close"); }
inline int VxIsVizionCamera(std::shared_ptr<VxCamera>) { return trace("verify"); }
inline int VxGetDeviceInterfaceType(std::shared_ptr<VxCamera>, VX_CAMERA_INTERFACE_TYPE& type) {
    type = scenario("usb") ? VX_CAMERA_INTERFACE_TYPE::INTERFACE_USB : VX_CAMERA_INTERFACE_TYPE::INTERFACE_MIPI_CSI2; return 0;
}
inline int VxGetDeviceName(std::shared_ptr<VxCamera>, std::string& name) { name = "TEVS-AR0822"; return 0; }
inline int VxGetHardwareID(std::shared_ptr<VxCamera>, std::string& id) { id = "test"; return 0; }
inline int VxGetFormatList(std::shared_ptr<VxCamera>, std::vector<VxFormat>& formats) {
    if (!scenario("empty_formats")) formats = {{7, 4, 2, 30, VX_IMAGE_FORMAT::UYVY}, {8, 4, 2, 30, VX_IMAGE_FORMAT::MJPG}};
    return trace("formats");
}
inline int VxSetFormat(std::shared_ptr<VxCamera>, VxFormat f) {
    if (f.mediatypeIdx != 7) return -1;
    return trace("set");
}
inline int VxStartStreaming(std::shared_ptr<VxCamera>) { return trace("start"); }
inline int VxStopStreaming(std::shared_ptr<VxCamera>) { return trace("stop"); }
inline VX_CAPTURE_RESULT VxGetImage(std::shared_ptr<VxCamera>, uint8_t* data, int* size, uint16_t) {
    trace("capture");
    static int calls = 0;
    ++calls;
    if (scenario("interrupt")) std::raise(SIGINT);
    if (scenario("timeout") || (scenario("recover") && calls == 1)) return VX_CAPTURE_RESULT::VX_TIMEOUT;
    if (scenario("corrupt")) return VX_CAPTURE_RESULT::VX_BUFFER_CORRUPTED;
    if (scenario("occupied")) return VX_CAPTURE_RESULT::VX_CAM_OCCUPIED;
    if (scenario("error")) return VX_CAPTURE_RESULT::VX_OTHER_ERROR;
    std::fill_n(data, 16, uint8_t{42});
    *size = scenario("bad_size") ? 15 : 16;
    return VX_CAPTURE_RESULT::VX_SUCCESS;
}
