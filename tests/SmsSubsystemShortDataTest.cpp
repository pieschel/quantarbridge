#include "SmsSubsystem.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>

namespace {
std::vector<uint8_t> encodeUtf16Be(const std::string& text)
{
    std::vector<uint8_t> bytes;
    bytes.reserve(text.size() * 2U);
    for (const unsigned char character : text) {
        bytes.push_back(0x00U);
        bytes.push_back(character);
    }
    return bytes;
}

size_t regularFileCount(const std::filesystem::path& path)
{
    if (!std::filesystem::exists(path)) {
        return 0U;
    }
    size_t count = 0U;
    for (const auto& entry : std::filesystem::directory_iterator(path)) {
        if (entry.is_regular_file()) {
            ++count;
        }
    }
    return count;
}
}

int main()
{
    const std::filesystem::path root = std::filesystem::temp_directory_path() /
        "quantarbridge-sms-short-data-test";
    std::error_code ec;
    std::filesystem::remove_all(root, ec);

    SmsConfig config;
    config.p25OutboxPath = (root / "p25-outbox").string();
    config.serviceRoutePath = (root / "service-routes").string();
    std::filesystem::create_directories(config.serviceRoutePath);

    const uint64_t nowMs = static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
    const auto routePath = std::filesystem::path(config.serviceRoutePath) / "weather.json";
    {
        std::ofstream route(routePath);
        route << "{\n"
              << "  \"createdAtMs\": " << nowMs << ",\n"
              << "  \"expiresAtMs\": " << nowMs + 60000U << ",\n"
              << "  \"serviceRid\": 262993,\n"
              << "  \"requesterRid\": 2621501\n"
              << "}\n";
    }

    SmsSubsystem sms(config);
    const std::string weather = "WX Report - Grafing\n27.86 C H:58%";
    const auto shortData = encodeUtf16Be(weather);
    if (!sms.handleBrandmeisterShortData(262993U, 999999U, shortData)) {
        std::cerr << "Defined Short Data weather reply was rejected\n";
        return 1;
    }
    if (regularFileCount(config.p25OutboxPath) != 1U || std::filesystem::exists(routePath)) {
        std::cerr << "Defined Short Data weather reply was not routed exactly once\n";
        return 1;
    }

    const auto queuedPath = std::filesystem::directory_iterator(config.p25OutboxPath)->path();
    std::ifstream queued(queuedPath);
    const std::string contents((std::istreambuf_iterator<char>(queued)),
        std::istreambuf_iterator<char>());
    if (contents.find("sourceRid: 262993") == std::string::npos ||
        contents.find("targetRid: 2621501") == std::string::npos ||
        contents.find("textHex: 5758205265706f7274202d2047726166696e670a32372e3836204320483a353825") ==
            std::string::npos) {
        std::cerr << "Defined Short Data weather event contents are invalid\n";
        return 1;
    }

    if (!sms.handleBrandmeisterShortData(262993U, 999999U, shortData) ||
        regularFileCount(config.p25OutboxPath) != 1U) {
        std::cerr << "Defined Short Data duplicate was queued more than once\n";
        return 1;
    }

    std::filesystem::remove_all(root, ec);
    return 0;
}
