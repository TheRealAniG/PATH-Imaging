#include "VizionSDK.h"

#include <csignal>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
namespace {
volatile std::sig_atomic_t interrupted = 0;
void on_signal(int) { interrupted = 1; }
void check(int result, const char* operation) {
    if (result != 0) throw std::runtime_error(std::string(operation) + " failed (" + std::to_string(result) + ")");
}
int number(const std::string& value, int minimum, int maximum) {
    if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos)
        throw std::runtime_error("Expected an unsigned integer: " + value);
    const auto parsed = std::stoull(value);
    if (parsed < static_cast<unsigned long long>(minimum) || parsed > static_cast<unsigned long long>(maximum))
        throw std::runtime_error("Value out of range: " + value);
    return static_cast<int>(parsed);
}
struct Options {
    bool list = false, formats = false, help = false;
    int device = -1, format = -1, frames = 1, timeout = 2500, retries = 3;
    fs::path output;
};
Options parse(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string key = argv[i];
        if (key == "--help") { o.help = true; continue; }
        if (key == "--list") { o.list = true; continue; }
        if (key == "--formats") { o.formats = true; continue; }
        if (key != "--device" && key != "--format" && key != "--frames" &&
            key != "--timeout-ms" && key != "--retries" && key != "--output")
            throw std::runtime_error("Unknown option: " + key);
        if (++i == argc) throw std::runtime_error("Missing value for " + key);
        const std::string value = argv[i];
        if (key == "--output") o.output = value;
        else if (key == "--device") o.device = number(value, 0, 65535);
        else if (key == "--format") o.format = number(value, 0, 65535);
        else if (key == "--frames") o.frames = number(value, 1, 1000000);
        else if (key == "--timeout-ms") o.timeout = number(value, 1, 65535);
        else o.retries = number(value, 0, 1000);
    }
    if (!o.help && !o.list) {
        if (o.device < 0) throw std::runtime_error("Select --device from --list first");
        if (!o.formats && (o.format < 0 || o.output.empty()))
            throw std::runtime_error("Capture requires --format and a new --output directory");
    }
    return o;
}
const char* format_name(VX_IMAGE_FORMAT format) {
    switch (format) {
    case VX_IMAGE_FORMAT::UYVY: return "UYVY";
    case VX_IMAGE_FORMAT::YUY2: return "YUY2";
    case VX_IMAGE_FORMAT::NV12: return "NV12";
    case VX_IMAGE_FORMAT::MJPG: return "MJPG";
    case VX_IMAGE_FORMAT::BGRA: return "BGRA";
    case VX_IMAGE_FORMAT::BGRX: return "BGRX";
    case VX_IMAGE_FORMAT::BGR: return "BGR";
    case VX_IMAGE_FORMAT::RGB16: return "RGB16";
    case VX_IMAGE_FORMAT::RGB: return "RGB";
    default: return "UNKNOWN";
    }
}
// Restrict capture to fixed-size formats: VxGetImage has no capacity argument.
// Compressed formats and undocumented row padding have no safe bound here.
std::size_t frame_size(const VxFormat& f) {
    if (!f.width || !f.height) throw std::runtime_error("Invalid zero-sized format");
    const std::size_t pixels = std::size_t(f.width) * f.height;
    std::size_t bytes = 0;
    switch (f.format) {
    case VX_IMAGE_FORMAT::UYVY:
    case VX_IMAGE_FORMAT::YUY2:
        if (f.width % 2) throw std::runtime_error("Packed YUV requires even width");
        bytes = pixels * 2; break;
    case VX_IMAGE_FORMAT::RGB16: bytes = pixels * 2; break;
    case VX_IMAGE_FORMAT::BGRA:
    case VX_IMAGE_FORMAT::BGRX: bytes = pixels * 4; break;
    case VX_IMAGE_FORMAT::BGR:
    case VX_IMAGE_FORMAT::RGB: bytes = pixels * 3; break;
    case VX_IMAGE_FORMAT::NV12:
        if (f.width % 2 || f.height % 2) throw std::runtime_error("NV12 requires even dimensions");
        bytes = pixels + pixels / 2; break;
    default: throw std::runtime_error("Capture supports uncompressed formats only; select a listed UYVY/YUY2/NV12/RGB format");
    }
    if (bytes > 256 * 1024 * 1024) throw std::runtime_error("Frame exceeds 256 MiB limit");
    return bytes;
}
class Camera {
public:
    std::shared_ptr<VxCamera> handle;
    bool streaming = false;
    explicit Camera(int index) : handle(VxInitialCameraDevice(index)) {
        if (!handle) throw std::runtime_error("Camera initialization failed");
        check(VxOpen(handle), "VxOpen");
    }
    Camera(const Camera&) = delete;
    Camera& operator=(const Camera&) = delete;
    ~Camera() {
        if (streaming && VxStopStreaming(handle) != 0) std::cerr << "Warning: stream cleanup failed\n";
        if (handle && VxClose(handle) != 0) std::cerr << "Warning: camera cleanup failed\n";
    }
    void finish() {
        if (streaming) {
            check(VxStopStreaming(handle), "VxStopStreaming");
            streaming = false;
        }
        check(VxClose(handle), "VxClose");
        handle.reset();
    }
};
int run(const Options& o) {
    if (o.help) {
        std::cout << "PATH Imaging TEVS-AR0822 host capture\n"
            "  path-camera --list\n"
            "  path-camera --device N --formats\n"
            "  path-camera --device N --format N --output NEW_DIRECTORY\n"
            "              [--frames 1] [--timeout-ms 2500] [--retries 3]\n"
            "Saves uncompressed SDK payloads plus frames.csv. Ctrl+C stops capture.\n";
        return 0;
    }
    std::vector<std::string> devices;
    const int count = VxDiscoverCameraDevices(devices);
    if (count < 0) throw std::runtime_error("Camera discovery failed");
    if (devices.empty()) throw std::runtime_error("No cameras found; check TEVS driver and media route");
    for (std::size_t i = 0; i < devices.size(); ++i) std::cout << '[' << i << "] " << devices[i] << '\n';
    if (o.list) return 0;
    if (std::size_t(o.device) >= devices.size()) throw std::runtime_error("Device index out of range");
    Camera camera(o.device);
    check(VxIsVizionCamera(camera.handle), "TechNexion camera verification");
    VX_CAMERA_INTERFACE_TYPE interface_type{};
    check(VxGetDeviceInterfaceType(camera.handle, interface_type), "VxGetDeviceInterfaceType");
    if (interface_type != VX_CAMERA_INTERFACE_TYPE::INTERFACE_MIPI_CSI2)
        throw std::runtime_error("The TEVS target requires a MIPI CSI-2 device");
    std::string name, hardware_id;
    check(VxGetDeviceName(camera.handle, name), "VxGetDeviceName");
    check(VxGetHardwareID(camera.handle, hardware_id), "VxGetHardwareID");
    std::cout << "Device: " << name << "\nHardware ID: " << hardware_id << '\n';
    std::vector<VxFormat> formats;
    check(VxGetFormatList(camera.handle, formats), "VxGetFormatList");
    if (formats.empty()) throw std::runtime_error("Camera reported no formats");
    for (std::size_t i = 0; i < formats.size(); ++i) {
        const auto& f = formats[i];
        std::cout << '[' << i << "] " << f.width << 'x' << f.height << ' ' << f.framerate
                  << " fps " << format_name(f.format) << " media=" << int(f.mediatypeIdx) << '\n';
    }
    if (o.formats) { camera.finish(); return 0; }
    if (std::size_t(o.format) >= formats.size()) throw std::runtime_error("Format index out of range");
    const auto selected = formats[o.format];
    const auto expected = frame_size(selected);
    // Extra headroom follows the vendor's width*height allocation model. The
    // SDK must deliver tightly packed data; size checks cannot prevent SDK writes.
    std::vector<uint8_t> buffer(std::size_t(selected.width) * selected.height * 4);
    check(VxSetFormat(camera.handle, selected), "VxSetFormat");
    if (!fs::create_directory(o.output)) throw std::runtime_error("Output directory already exists; choose a new directory");
    std::ofstream metadata(o.output / "frames.csv");
    metadata.exceptions(std::ios::failbit | std::ios::badbit);
    metadata << "file,width,height,fps,pixel_format,media_type,bytes\n";
    check(VxStartStreaming(camera.handle), "VxStartStreaming");
    camera.streaming = true;
    int captured = 0, failures = 0;
    while (captured < o.frames && !interrupted) {
        int size = 0; // SDK output parameter, not buffer capacity.
        const auto result = VxGetImage(camera.handle, buffer.data(), &size, static_cast<uint16_t>(o.timeout));
        if (interrupted) break;
        if (result == VX_CAPTURE_RESULT::VX_TIMEOUT || result == VX_CAPTURE_RESULT::VX_BUFFER_CORRUPTED) {
            std::cerr << "Frame rejected (" << static_cast<int>(result) << ")\n";
            if (++failures > o.retries) throw std::runtime_error("Consecutive capture retry limit exceeded");
            continue;
        }
        if (result != VX_CAPTURE_RESULT::VX_SUCCESS)
            throw std::runtime_error("Capture failed (" + std::to_string(static_cast<int>(result)) + ")");
        if (size <= 0 || static_cast<std::size_t>(size) != expected)
            throw std::runtime_error("Unexpected frame size; packed format/stride contract not satisfied");
        failures = 0;
        const auto filename = "frame_" + std::to_string(captured) + ".raw";
        std::ofstream frame(o.output / filename, std::ios::binary);
        frame.exceptions(std::ios::failbit | std::ios::badbit);
        frame.write(reinterpret_cast<const char*>(buffer.data()), size);
        frame.close();
        metadata << filename << ',' << selected.width << ',' << selected.height << ','
                 << selected.framerate << ',' << format_name(selected.format) << ','
                 << int(selected.mediatypeIdx) << ',' << size << '\n';
        metadata.flush();
        ++captured;
        std::cout << "Saved " << filename << " (" << size << " bytes)\n";
    }
    metadata.close();
    camera.finish();
    return interrupted ? 130 : 0;
}
} // namespace
int main(int argc, char** argv) {
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    try { return run(parse(argc, argv)); }
    catch (const std::exception& error) {
        std::cerr << "Error: " << error.what() << '\n';
        return 1;
    }
}
