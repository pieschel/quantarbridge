#include "SmsSubsystem.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {
std::vector<uint8_t> fromHex(const std::string& hex)
{
    if ((hex.size() & 1U) != 0U) {
        throw std::runtime_error("odd hex length");
    }

    std::vector<uint8_t> bytes;
    bytes.reserve(hex.size() / 2U);
    for (size_t i = 0U; i < hex.size(); i += 2U) {
        bytes.push_back(static_cast<uint8_t>(std::stoul(hex.substr(i, 2U), nullptr, 16)));
    }
    return bytes;
}

struct CapturedPacket {
    uint32_t sourceRid {0U};
    uint32_t targetRid {0U};
    uint8_t slotNo {0U};
    std::vector<uint8_t> bytes;
};

bool hasRegularFiles(const std::filesystem::path& path)
{
    if (!std::filesystem::exists(path)) {
        return false;
    }
    return std::any_of(std::filesystem::directory_iterator(path),
        std::filesystem::directory_iterator(),
        [](const std::filesystem::directory_entry& entry) { return entry.is_regular_file(); });
}
}

int main()
{
    const std::filesystem::path root = std::filesystem::temp_directory_path() /
        "quantarbridge-sms-brandmeister-test";
    std::error_code ec;
    std::filesystem::remove_all(root, ec);

    SmsConfig config;
    config.enabled = true;
    config.bindAddress = "127.0.0.1";
    config.arsPort = 0U;
    config.tmsPort = 0U;
    config.outboundMode = "brandmeister";
    config.bmSourceIp = "auto";
    config.bmTargetIp = "auto";
    config.pollIntervalMs = 1U;
    config.inboxPath = (root / "inbox").string();
    config.outboxPath = (root / "outbox").string();
    config.sentPath = (root / "sent").string();
    config.p25OutboxPath = (root / "p25-outbox").string();
    config.serviceRoutePath = (root / "service-routes").string();
    SmsSubsystem sms(config);

    std::vector<CapturedPacket> outbound;
    sms.setBmPacketDataWriter([&outbound](uint32_t sourceRid, uint32_t targetRid,
        uint8_t slotNo, const std::vector<uint8_t>& packet) {
        outbound.push_back({sourceRid, targetRid, slotNo, packet});
        return true;
    });
    if (!sms.open()) {
        std::cerr << "SMS subsystem did not open for outbound test\n";
        return 1;
    }

    const auto controlPacket = fromHex(
        "450000224a170000401194130c0f42410c0f42420fa70fa7000ef67d0004bf008e60");
    if (!sms.handleBrandmeisterPacketData(1000001U, 1000002U, 2U, controlPacket) ||
        !outbound.empty() || hasRegularFiles(config.p25OutboxPath)) {
        std::cerr << "TMS control response was treated as a text message\n";
        return 1;
    }

    const auto textPacket = fromHex(
        "450000304a180000401194040c0f42410c0f42420fa70fa7001ce4ad"
        "0012e00080040d000a0054006500730074003200");
    if (!sms.handleBrandmeisterPacketData(1000001U, 1000002U, 2U, textPacket)) {
        std::cerr << "valid TMS text packet was rejected\n";
        return 1;
    }
    if (outbound.size() != 1U || outbound[0U].sourceRid != 1000002U ||
        outbound[0U].targetRid != 1000001U || outbound[0U].slotNo != 2U) {
        std::cerr << "TMS acknowledgement routing is invalid\n";
        return 1;
    }

    const auto& acknowledgement = outbound[0U].bytes;
    const std::vector<uint8_t> expectedPayload = {0x00U, 0x03U, 0xBFU, 0x00U, 0x00U};
    if (acknowledgement.size() != 33U ||
        !std::equal(expectedPayload.begin(), expectedPayload.end(), acknowledgement.begin() + 28U) ||
        !std::equal(textPacket.begin() + 16U, textPacket.begin() + 20U, acknowledgement.begin() + 12U) ||
        !std::equal(textPacket.begin() + 12U, textPacket.begin() + 16U, acknowledgement.begin() + 16U)) {
        std::cerr << "TMS acknowledgement packet is invalid\n";
        return 1;
    }

    std::vector<std::filesystem::path> queuedFiles;
    for (const auto& entry : std::filesystem::directory_iterator(config.p25OutboxPath)) {
        if (entry.is_regular_file()) {
            queuedFiles.push_back(entry.path());
        }
    }
    if (queuedFiles.size() != 1U) {
        std::cerr << "TMS text was not queued exactly once\n";
        return 1;
    }

    std::ifstream queued(queuedFiles.front());
    const std::string contents((std::istreambuf_iterator<char>(queued)),
        std::istreambuf_iterator<char>());
    if (contents.find("sourceRid: 1000001") == std::string::npos ||
        contents.find("targetRid: 1000002") == std::string::npos ||
        contents.find("textHex: 5465737432") == std::string::npos) {
        std::cerr << "queued TMS text is invalid\n";
        return 1;
    }

    auto retransmittedTextPacket = textPacket;
    retransmittedTextPacket[32U] = 0x81U;
    outbound.clear();
    if (!sms.handleBrandmeisterPacketData(1000001U, 1000002U, 2U, retransmittedTextPacket) ||
        outbound.size() != 1U) {
        std::cerr << "E0 TMS retransmission was not acknowledged\n";
        return 1;
    }
    size_t queuedAfterRetransmission = 0U;
    for (const auto& entry : std::filesystem::directory_iterator(config.p25OutboxPath)) {
        if (entry.is_regular_file()) {
            ++queuedAfterRetransmission;
        }
    }
    if (queuedAfterRetransmission != 1U) {
        std::cerr << "E0 TMS retransmission bypassed text deduplication\n";
        return 1;
    }

    std::filesystem::remove(queuedFiles.front(), ec);
    const uint64_t nowMs = static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
    const auto serviceRoutePath = std::filesystem::path(config.serviceRoutePath) /
        (std::to_string(nowMs) + "-1000001-1000003-test.json");
    const auto secondServiceRoutePath = std::filesystem::path(config.serviceRoutePath) /
        (std::to_string(nowMs + 1U) + "-1000001-1000004-test.json");
    {
        std::ofstream route(serviceRoutePath);
        route
            << "{\n"
            << "  \"createdAtMs\": " << nowMs << ",\n"
            << "  \"expiresAtMs\": " << nowMs + 60000U << ",\n"
            << "  \"serviceRid\": 1000001,\n"
            << "  \"requesterRid\": 1000003\n"
            << "}\n";
    }
    {
        std::ofstream route(secondServiceRoutePath);
        route
            << "{\n"
            << "  \"createdAtMs\": " << nowMs + 1U << ",\n"
            << "  \"expiresAtMs\": " << nowMs + 60000U << ",\n"
            << "  \"serviceRid\": 1000001,\n"
            << "  \"requesterRid\": 1000004\n"
            << "}\n";
    }

    auto routedTextPacket = textPacket;
    routedTextPacket[routedTextPacket.size() - 2U] = 0x33U;
    outbound.clear();
    if (!sms.handleBrandmeisterPacketData(1000001U, 1000002U, 2U, routedTextPacket)) {
        std::cerr << "routed TMS service reply was rejected\n";
        return 1;
    }
    if (outbound.size() != 1U || outbound[0U].sourceRid != 1000002U ||
        outbound[0U].targetRid != 1000001U || outbound[0U].slotNo != 2U) {
        std::cerr << "routed TMS acknowledgement did not preserve network addressing\n";
        return 1;
    }
    queuedFiles.clear();
    for (const auto& entry : std::filesystem::directory_iterator(config.p25OutboxPath)) {
        if (entry.is_regular_file()) {
            queuedFiles.push_back(entry.path());
        }
    }
    if (queuedFiles.size() != 1U || std::filesystem::exists(serviceRoutePath) ||
        !std::filesystem::exists(secondServiceRoutePath)) {
        std::cerr << "TMS service route was not consumed exactly once\n";
        return 1;
    }
    std::ifstream routedQueued(queuedFiles.front());
    const std::string routedContents((std::istreambuf_iterator<char>(routedQueued)),
        std::istreambuf_iterator<char>());
    if (routedContents.find("sourceRid: 1000001") == std::string::npos ||
        routedContents.find("targetRid: 1000003") == std::string::npos ||
        routedContents.find("textHex: 5465737433") == std::string::npos) {
        std::cerr << "TMS service reply was queued for the wrong requester\n";
        return 1;
    }

    outbound.clear();
    if (!sms.handleBrandmeisterPacketData(1000001U, 1000002U, 2U, routedTextPacket) ||
        outbound.size() != 1U) {
        std::cerr << "duplicate routed TMS service reply was not acknowledged\n";
        return 1;
    }
    size_t queuedAfterDuplicate = 0U;
    for (const auto& entry : std::filesystem::directory_iterator(config.p25OutboxPath)) {
        if (entry.is_regular_file()) {
            ++queuedAfterDuplicate;
        }
    }
    if (queuedAfterDuplicate != 1U) {
        std::cerr << "duplicate routed TMS service reply was queued again\n";
        return 1;
    }
    if (!std::filesystem::exists(secondServiceRoutePath)) {
        std::cerr << "duplicate TMS reply consumed the next request route\n";
        return 1;
    }

    auto secondRoutedTextPacket = textPacket;
    secondRoutedTextPacket[secondRoutedTextPacket.size() - 2U] = 0x34U;
    outbound.clear();
    if (!sms.handleBrandmeisterPacketData(1000001U, 1000002U, 2U, secondRoutedTextPacket) ||
        outbound.size() != 1U || std::filesystem::exists(secondServiceRoutePath)) {
        std::cerr << "second TMS service route was not consumed correctly\n";
        return 1;
    }
    bool foundSecondRequester = false;
    for (const auto& entry : std::filesystem::directory_iterator(config.p25OutboxPath)) {
        if (!entry.is_regular_file()) {
            continue;
        }
        std::ifstream candidate(entry.path());
        const std::string candidateContents((std::istreambuf_iterator<char>(candidate)),
            std::istreambuf_iterator<char>());
        foundSecondRequester = foundSecondRequester ||
            candidateContents.find("targetRid: 1000004") != std::string::npos;
    }
    if (!foundSecondRequester) {
        std::cerr << "second TMS service reply was queued for the wrong requester\n";
        return 1;
    }

    std::filesystem::remove_all(config.p25OutboxPath, ec);
    std::filesystem::create_directories(config.p25OutboxPath, ec);

    outbound.clear();
    const auto outboundPath = std::filesystem::path(config.outboxPath) / "apx-direct.json";
    {
        std::ofstream queuedOutbound(outboundPath);
        queuedOutbound
            << "route: brandmeister\n"
            << "channel: tms\n"
            << "sourceRid: 1000002\n"
            << "targetRid: 1000001\n"
            << "text: Direkttest4\n";
    }
    sms.clock();

    if (outbound.size() != 1U || outbound[0U].sourceRid != 1000002U ||
        outbound[0U].targetRid != 1000001U || outbound[0U].slotNo != 2U) {
        std::cerr << "outbound TMS routing is invalid\n";
        return 1;
    }

    const auto& directPacket = outbound[0U].bytes;
    const std::vector<uint8_t> expectedAddresses = {
        12U, 15U, 66U, 66U, 12U, 15U, 66U, 65U
    };
    const std::vector<uint8_t> expectedDirectPayload = {
        0x00U, 0x1EU, 0xE0U, 0x00U, 0x80U, 0x04U,
        0x0DU, 0x00U, 0x0AU, 0x00U,
        0x44U, 0x00U, 0x69U, 0x00U, 0x72U, 0x00U, 0x65U, 0x00U,
        0x6BU, 0x00U, 0x74U, 0x00U, 0x74U, 0x00U, 0x65U, 0x00U,
        0x73U, 0x00U, 0x74U, 0x00U, 0x34U, 0x00U
    };
    if (directPacket.size() != 60U ||
        !std::equal(expectedAddresses.begin(), expectedAddresses.end(), directPacket.begin() + 12U) ||
        directPacket[20U] != 0x0FU || directPacket[21U] != 0xA7U ||
        directPacket[22U] != 0x0FU || directPacket[23U] != 0xA7U ||
        !std::equal(expectedDirectPayload.begin(), expectedDirectPayload.end(), directPacket.begin() + 28U)) {
        std::cerr << "outbound BrandMeister IPv4/TMS packet is invalid\n";
        return 1;
    }
    if (std::filesystem::exists(outboundPath) ||
        !std::filesystem::exists(std::filesystem::path(config.sentPath) / "apx-direct.json.sent")) {
        std::cerr << "outbound TMS file was not archived\n";
        return 1;
    }

    outbound.clear();
    const std::string lrrpHex =
        "4500002d0001000040111477c633640acb00710a"
        "c02ec02e0019c2f3070f220400000001662266666607d27d28";
    const auto lrrpPath = std::filesystem::path(config.outboxPath) / "apx-location.json";
    {
        std::ofstream queuedLocation(lrrpPath);
        queuedLocation
            << "route: brandmeister\n"
            << "channel: lrrp\n"
            << "sourceRid: 1000002\n"
            << "targetRid: 262999\n"
            << "rawIpPacketHex: " << lrrpHex << "\n";
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
    sms.clock();

    if (outbound.size() != 1U || outbound[0U].sourceRid != 1000002U ||
        outbound[0U].targetRid != 262999U || outbound[0U].slotNo != 2U) {
        std::cerr << "outbound LRRP routing is invalid\n";
        return 1;
    }

    const auto& lrrpPacket = outbound[0U].bytes;
    const auto originalLrrp = fromHex(lrrpHex);
    const std::vector<uint8_t> expectedLocationAddresses = {
        12U, 15U, 66U, 66U, 12U, 4U, 3U, 87U
    };
    if (lrrpPacket.size() != 45U ||
        !std::equal(expectedLocationAddresses.begin(), expectedLocationAddresses.end(), lrrpPacket.begin() + 12U) ||
        lrrpPacket[20U] != 0x0FU || lrrpPacket[21U] != 0xA1U ||
        lrrpPacket[22U] != 0x0FU || lrrpPacket[23U] != 0xA1U ||
        !std::equal(originalLrrp.begin() + 28U, originalLrrp.end(), lrrpPacket.begin() + 28U)) {
        std::cerr << "outbound BrandMeister IPv4/LRRP packet is invalid\n";
        return 1;
    }
    if (std::filesystem::exists(lrrpPath) ||
        !std::filesystem::exists(std::filesystem::path(config.sentPath) / "apx-location.json.sent")) {
        std::cerr << "outbound LRRP file was not archived\n";
        return 1;
    }

    outbound.clear();
    const std::string noFixLrrpHex =
        "4500002746be00004011cdbfc633640acb00710a"
        "0fa10fa1001319aa0709220400000002378400";
    const auto noFixLrrpPath = std::filesystem::path(config.outboxPath) / "apx-location-no-fix.json";
    {
        std::ofstream queuedNoFixLocation(noFixLrrpPath);
        queuedNoFixLocation
            << "route: brandmeister\n"
            << "channel: lrrp\n"
            << "sourceRid: 1000002\n"
            << "targetRid: 262999\n"
            << "rawIpPacketHex: " << noFixLrrpHex << "\n";
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
    sms.clock();

    if (!outbound.empty()) {
        std::cerr << "LRRP no-fix status was forwarded to BrandMeister\n";
        return 1;
    }
    if (std::filesystem::exists(noFixLrrpPath) ||
        !std::filesystem::exists(std::filesystem::path(config.sentPath) / "apx-location-no-fix.json.sent")) {
        std::cerr << "LRRP no-fix status was not archived\n";
        return 1;
    }

    sms.close();
    std::filesystem::remove_all(root, ec);
    return 0;
}
