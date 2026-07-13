#include "SmsSubsystem.h"

#include "common/Log.h"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cctype>
#include <codecvt>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <locale>
#include <sstream>
#include <system_error>
#include <utility>

#if !defined(_WIN32)
#include <arpa/inet.h>
#include <netinet/in.h>
#endif

using namespace network::udp;

namespace {

constexpr uint32_t kMaxInboundPacketsPerClock = 16U;
constexpr size_t kMaxOutboundFilesPerPoll = 8U;
constexpr auto kBmTmsReplyDuplicateWindow = std::chrono::seconds(60);

std::string trim(const std::string& value)
{
    const auto start = value.find_first_not_of(" \t\r\n");
    if (start == std::string::npos) {
        return {};
    }

    const auto end = value.find_last_not_of(" \t\r\n");
    return value.substr(start, end - start + 1U);
}

bool isPrintableUtf16(uint16_t value)
{
    if (value == u'\r' || value == u'\n' || value == u'\t') {
        return true;
    }

    if (value >= 0x20U && value <= 0x7EU) {
        return true;
    }

    return value >= 0x00A0U && !(value >= 0xD800U && value <= 0xDFFFU);
}

bool equalsIgnoreCase(const std::string& left, const char* right)
{
    const std::string rightText(right);
    if (left.size() != rightText.size()) {
        return false;
    }

    for (size_t i = 0U; i < left.size(); ++i) {
        if (std::tolower(static_cast<unsigned char>(left[i])) != std::tolower(static_cast<unsigned char>(rightText[i]))) {
            return false;
        }
    }
    return true;
}

bool parseIpv4Address(const std::string& value, std::array<uint8_t, 4U>& output)
{
    std::istringstream stream(value);
    std::string part;
    for (size_t i = 0U; i < output.size(); ++i) {
        if (!std::getline(stream, part, '.')) {
            return false;
        }
        if (part.empty() || part.size() > 3U ||
            std::any_of(part.begin(), part.end(), [](unsigned char c) { return !std::isdigit(c); })) {
            return false;
        }

        const unsigned long octet = std::stoul(part);
        if (octet > 255UL) {
            return false;
        }
        output[i] = static_cast<uint8_t>(octet);
    }

    return !std::getline(stream, part, '.');
}

uint16_t readUint16(const uint8_t* data)
{
    return static_cast<uint16_t>((static_cast<uint16_t>(data[0U]) << 8U) |
        data[1U]);
}

bool skipMotorolaVlq(const uint8_t* data, size_t length, size_t& offset)
{
    for (size_t i = 0U; i < 5U && offset < length; ++i, ++offset) {
        if ((data[offset] & 0x80U) == 0U) {
            ++offset;
            return true;
        }
    }
    return false;
}

bool extractMotorolaLrrpPayload(const std::vector<uint8_t>& packet,
    std::vector<uint8_t>& payload, bool& hasPosition)
{
    hasPosition = false;
    if (packet.size() < 30U || (packet[0U] >> 4U) != 4U ||
        packet[9U] != 0x11U) {
        return false;
    }

    const size_t ipHeaderLength = static_cast<size_t>(packet[0U] & 0x0FU) * 4U;
    const size_t packetLength = readUint16(packet.data() + 2U);
    if (ipHeaderLength < 20U || packetLength > packet.size() ||
        packetLength < ipHeaderLength + 10U) {
        return false;
    }

    const uint8_t* udp = packet.data() + ipHeaderLength;
    const uint16_t sourcePort = readUint16(udp);
    const uint16_t targetPort = readUint16(udp + 2U);
    const size_t udpLength = readUint16(udp + 4U);
    const bool locationPorts =
        (sourcePort == 49198U && targetPort == 49198U) ||
        (sourcePort == 4001U && targetPort == 4001U);
    if (!locationPorts || udpLength < 10U ||
        ipHeaderLength + udpLength > packetLength) {
        return false;
    }

    const uint8_t* lrrp = udp + 8U;
    const uint8_t messageType = static_cast<uint8_t>(lrrp[0U] & 0x7FU);
    if (messageType != 0x07U && messageType != 0x0DU) {
        return false;
    }

    const size_t lrrpLength = udpLength - 8U;
    if (lrrpLength < 2U || static_cast<size_t>(lrrp[1U]) + 2U > lrrpLength) {
        return false;
    }

    const size_t reportLength = static_cast<size_t>(lrrp[1U]) + 2U;
    size_t offset = 2U;
    if (offset + 2U <= reportLength && lrrp[offset] == 0x22U) {
        const size_t requestIdLength = lrrp[offset + 1U];
        if (offset + 2U + requestIdLength > reportLength) {
            return false;
        }
        offset += 2U + requestIdLength;
    }

    while (offset < reportLength) {
        switch (lrrp[offset]) {
        case 0x34U:
            if (offset + 6U > reportLength) {
                return false;
            }
            offset += 6U;
            break;
        case 0x37U:
            ++offset;
            if (!skipMotorolaVlq(lrrp, reportLength, offset)) {
                return false;
            }
            break;
        case 0x51U:
        case 0x55U:
        case 0x66U:
        case 0x69U:
            if (offset + 9U > reportLength) {
                return false;
            }
            hasPosition = true;
            offset = reportLength;
            break;
        case 0x56U:
            if (offset + 2U > reportLength) {
                return false;
            }
            offset += 2U;
            break;
        case 0x6CU:
            ++offset;
            if (!skipMotorolaVlq(lrrp, reportLength, offset) ||
                !skipMotorolaVlq(lrrp, reportLength, offset)) {
                return false;
            }
            break;
        default:
            offset = reportLength;
            break;
        }
    }

    payload.assign(lrrp, udp + udpLength);
    return true;
}

std::string motorolaRadioIp(uint32_t rid)
{
    if (rid == 0U || rid > 0x00FFFFFFU) {
        return {};
    }

    std::ostringstream stream;
    stream << "12."
           << ((rid >> 16U) & 0xFFU) << '.'
           << ((rid >> 8U) & 0xFFU) << '.'
           << (rid & 0xFFU);
    return stream.str();
}

void writeUint16(std::vector<uint8_t>& buffer, size_t offset, uint16_t value)
{
    buffer[offset] = static_cast<uint8_t>(value >> 8);
    buffer[offset + 1U] = static_cast<uint8_t>(value);
}

uint32_t checksumAccumulate(const uint8_t* data, size_t length, uint32_t sum = 0U)
{
    for (size_t i = 0U; i + 1U < length; i += 2U) {
        sum += (static_cast<uint16_t>(data[i]) << 8U) | data[i + 1U];
    }
    if ((length & 1U) != 0U) {
        sum += static_cast<uint16_t>(data[length - 1U]) << 8U;
    }
    return sum;
}

uint16_t checksumFinalize(uint32_t sum)
{
    while ((sum >> 16U) != 0U) {
        sum = (sum & 0xFFFFU) + (sum >> 16U);
    }
    return static_cast<uint16_t>(~sum);
}

}

SmsSubsystem::SmsSubsystem(const SmsConfig& config) :
    m_config(config),
    m_arsSocket(config.bindAddress, config.arsPort),
    m_tmsSocket(config.bindAddress, config.tmsPort),
    m_txSocket(0U),
    m_rxBuffer(std::max<uint32_t>(config.maxPacketBytes, 512U), 0x00U)
{
}

bool SmsSubsystem::open()
{
    if (!m_config.enabled) {
        return true;
    }

    ensureDirectories();

    if (!m_arsSocket.open()) {
        ::LogError(LOG_HOST, "SMS subsystem failed to open ARS socket on %s:%u",
            m_config.bindAddress.c_str(), m_config.arsPort);
        return false;
    }

    if (!m_tmsSocket.open()) {
        ::LogError(LOG_HOST, "SMS subsystem failed to open TMS socket on %s:%u",
            m_config.bindAddress.c_str(), m_config.tmsPort);
        m_arsSocket.close();
        return false;
    }

    if (Socket::lookup(m_config.outboundAddress, m_config.outboundArsPort, m_arsTarget.storage, m_arsTarget.length) == 0) {
        m_arsTarget.valid = true;
    } else {
        ::LogWarning(LOG_HOST, "SMS subsystem could not resolve outbound ARS target %s:%u",
            m_config.outboundAddress.c_str(), m_config.outboundArsPort);
    }

    if (Socket::lookup(m_config.outboundAddress, m_config.outboundTmsPort, m_tmsTarget.storage, m_tmsTarget.length) == 0) {
        m_tmsTarget.valid = true;
    } else {
        ::LogWarning(LOG_HOST, "SMS subsystem could not resolve outbound TMS target %s:%u",
            m_config.outboundAddress.c_str(), m_config.outboundTmsPort);
    }

    if (!openTransmitSocket()) {
        m_arsSocket.close();
        m_tmsSocket.close();
        return false;
    }

    m_nextOutboxPoll = std::chrono::steady_clock::now();
    m_open = true;

    ::LogInfoEx(LOG_HOST, "SMS subsystem listening on %s:%u (ARS) and %s:%u (TMS), inbox=%s outbox=%s",
        m_config.bindAddress.c_str(), m_config.arsPort,
        m_config.bindAddress.c_str(), m_config.tmsPort,
        m_config.inboxPath.c_str(), m_config.outboxPath.c_str());

    return true;
}

void SmsSubsystem::close()
{
    m_open = false;
    m_txSocket.close();
    m_tmsSocket.close();
    m_arsSocket.close();
}

void SmsSubsystem::clock()
{
    if (!m_open) {
        return;
    }

    processIncomingSocket(m_arsSocket, "ars");
    processIncomingSocket(m_tmsSocket, "tms");

    const auto now = std::chrono::steady_clock::now();
    if (now >= m_nextOutboxPoll) {
        processOutboundQueue();
        m_nextOutboxPoll = now + std::chrono::milliseconds(std::max<uint32_t>(1U, m_config.pollIntervalMs));
    }
}

bool SmsSubsystem::isEnabled() const
{
    return m_config.enabled;
}

void SmsSubsystem::setBmPacketDataWriter(std::function<bool(uint32_t, uint32_t, uint8_t, const std::vector<uint8_t>&)> writer)
{
    m_bmPacketDataWriter = std::move(writer);
}

bool SmsSubsystem::handleBrandmeisterPacketData(uint32_t sourceRid, uint32_t targetRid,
    uint8_t slotNo, const std::vector<uint8_t>& ipv4Packet)
{
    if (ipv4Packet.size() < 28U || (ipv4Packet[0U] >> 4U) != 4U || ipv4Packet[9U] != 0x11U) {
        return false;
    }

    const size_t ipHeaderLength = static_cast<size_t>(ipv4Packet[0U] & 0x0FU) * 4U;
    const size_t ipTotalLength = (static_cast<size_t>(ipv4Packet[2U]) << 8U) | ipv4Packet[3U];
    if (ipHeaderLength < 20U || ipHeaderLength + 8U > ipv4Packet.size() ||
        ipTotalLength < ipHeaderLength + 8U || ipTotalLength > ipv4Packet.size()) {
        return false;
    }

    const size_t udpOffset = ipHeaderLength;
    const uint16_t sourcePort = static_cast<uint16_t>((ipv4Packet[udpOffset] << 8U) | ipv4Packet[udpOffset + 1U]);
    const uint16_t targetPort = static_cast<uint16_t>((ipv4Packet[udpOffset + 2U] << 8U) | ipv4Packet[udpOffset + 3U]);
    const size_t udpLength = (static_cast<size_t>(ipv4Packet[udpOffset + 4U]) << 8U) | ipv4Packet[udpOffset + 5U];
    if (udpLength < 8U || udpOffset + udpLength > ipTotalLength) {
        return false;
    }

    const uint8_t* tmsPayload = ipv4Packet.data() + udpOffset + 8U;
    const uint32_t tmsLength = static_cast<uint32_t>(udpLength - 8U);
    if (tmsLength >= 3U && (tmsPayload[2U] & 0x1FU) == 0x1FU) {
        const uint8_t messageReference = tmsLength > 4U ? tmsPayload[4U] & 0x1FU : 0U;
        const uint8_t deliveryStatus = tmsLength > 5U ? tmsPayload[5U] : 0U;
        ::LogInfoEx(LOG_HOST,
            "BM Motorola TMS control response consumed, srcRid=%u dstRid=%u operation=$%02X messageReference=%u status=$%02X",
            sourceRid, targetRid, tmsPayload[2U], messageReference, deliveryStatus);
        return true;
    }

    const auto parsed = parseMotorolaTms(tmsPayload, tmsLength);
    std::string text;
    if (parsed.has_value()) {
        text = !parsed->text.empty() ? parsed->text : parsed->textFragment;
    }
    if (text.empty()) {
        text = extractText(tmsPayload, tmsLength);
    }
    text = trim(text);
    if (text.empty()) {
        ::LogWarning(LOG_HOST,
            "BM packet-data did not contain a decodable Motorola TMS message, srcRid=%u dstRid=%u ports=%u->%u ipHex=%s",
            sourceRid, targetRid, sourcePort, targetPort,
            bytesToHex(ipv4Packet.data(), static_cast<uint32_t>(ipv4Packet.size())).c_str());
        return false;
    }

    const auto now = std::chrono::steady_clock::now();
    for (auto it = m_recentBmTmsReplies.begin(); it != m_recentBmTmsReplies.end();) {
        if (now >= it->second) {
            it = m_recentBmTmsReplies.erase(it);
        } else {
            ++it;
        }
    }
    const std::string replyKey = std::to_string(sourceRid) + ">" + std::to_string(targetRid) + ":" + text;
    const auto duplicate = m_recentBmTmsReplies.find(replyKey);
    if (duplicate != m_recentBmTmsReplies.end() && now < duplicate->second) {
        if (parsed.has_value() && !sendBrandmeisterTmsAcknowledgement(sourceRid, targetRid, slotNo,
            ipv4Packet, sourcePort, targetPort, parsed->operation, parsed->messageId)) {
            ::LogWarning(LOG_HOST, "BM Motorola TMS duplicate acknowledgement failed, srcRid=%u dstRid=%u messageId=%u",
                sourceRid, targetRid, parsed->messageId);
        }
        ::LogInfoEx(LOG_HOST, "Dropping duplicate BM Motorola TMS reply srcRid=%u dstRid=%u textLength=%u",
            sourceRid, targetRid, static_cast<uint32_t>(text.size()));
        return true;
    }

    std::error_code ec;
    std::filesystem::create_directories(m_config.p25OutboxPath, ec);
    if (ec) {
        ::LogWarning(LOG_HOST, "P25 TMS outbox directory %s could not be created: %s",
            m_config.p25OutboxPath.c_str(), ec.message().c_str());
        return false;
    }

    const std::string stem = makeEventStem("bm-tms");
    const auto targetPath = std::filesystem::path(m_config.p25OutboxPath) / (stem + ".yaml");
    const auto tempPath = targetPath.string() + ".tmp";
    std::ofstream output(tempPath, std::ios::trunc | std::ios::binary);
    if (!output.is_open()) {
        ::LogWarning(LOG_HOST, "P25 TMS outbox file %s could not be opened", tempPath.c_str());
        return false;
    }

    output << "sourceRid: " << sourceRid << '\n';
    output << "targetRid: " << targetRid << '\n';
    output << "textHex: " << bytesToHex(reinterpret_cast<const uint8_t*>(text.data()),
        static_cast<uint32_t>(text.size())) << '\n';
    output.close();
    if (!output) {
        std::filesystem::remove(tempPath, ec);
        return false;
    }

    std::filesystem::rename(tempPath, targetPath, ec);
    if (ec) {
        ::LogWarning(LOG_HOST, "P25 TMS outbox event %s could not be published: %s",
            targetPath.string().c_str(), ec.message().c_str());
        std::filesystem::remove(tempPath, ec);
        return false;
    }

    m_recentBmTmsReplies[replyKey] = now + kBmTmsReplyDuplicateWindow;

    if (parsed.has_value() && !sendBrandmeisterTmsAcknowledgement(sourceRid, targetRid, slotNo,
        ipv4Packet, sourcePort, targetPort, parsed->operation, parsed->messageId)) {
        ::LogWarning(LOG_HOST, "BM Motorola TMS acknowledgement failed, srcRid=%u dstRid=%u messageId=%u",
            sourceRid, targetRid, parsed->messageId);
    }

    ::LogInfoEx(LOG_HOST,
        "BM Motorola TMS reply queued for P25, srcRid=%u dstRid=%u ports=%u->%u textLength=%u file=%s",
        sourceRid, targetRid, sourcePort, targetPort, static_cast<uint32_t>(text.size()),
        targetPath.string().c_str());
    return true;
}

bool SmsSubsystem::sendBrandmeisterTmsAcknowledgement(uint32_t sourceRid, uint32_t targetRid,
    uint8_t slotNo, const std::vector<uint8_t>& ipv4Packet, uint16_t sourcePort,
    uint16_t targetPort, uint8_t requestOperation, uint8_t messageId)
{
    if (!m_bmPacketDataWriter || ipv4Packet.size() < 20U) {
        return false;
    }

    auto addressToString = [](const uint8_t* address) {
        std::ostringstream output;
        output << static_cast<uint32_t>(address[0U]) << '.'
               << static_cast<uint32_t>(address[1U]) << '.'
               << static_cast<uint32_t>(address[2U]) << '.'
               << static_cast<uint32_t>(address[3U]);
        return output.str();
    };

    const uint8_t acknowledgementOperation = static_cast<uint8_t>(
        0x9FU | (requestOperation & 0x20U));
    const std::vector<uint8_t> acknowledgement = {
        0x00U, 0x03U, acknowledgementOperation, 0x00U,
        static_cast<uint8_t>(messageId & 0x1FU)
    };
    const std::vector<uint8_t> acknowledgementPacket = buildIpv4UdpPacket(
        addressToString(ipv4Packet.data() + 16U),
        addressToString(ipv4Packet.data() + 12U),
        targetPort, sourcePort, acknowledgement);
    if (acknowledgementPacket.empty() ||
        !m_bmPacketDataWriter(targetRid, sourceRid, slotNo, acknowledgementPacket)) {
        return false;
    }

    ::LogInfoEx(LOG_HOST,
        "BM Motorola TMS acknowledgement sent, srcRid=%u dstRid=%u slot=%u operation=$%02X messageId=%u",
        targetRid, sourceRid, slotNo, acknowledgementOperation, messageId & 0x1FU);
    return true;
}

std::optional<SmsSubsystem::ParsedSmsPacket> SmsSubsystem::parseMotorolaPacket(const char* channelName, const uint8_t* data, uint32_t length) const
{
    if (std::strcmp(channelName, "ars") == 0) {
        return parseMotorolaArs(data, length);
    }
    if (std::strcmp(channelName, "tms") == 0) {
        return parseMotorolaTms(data, length);
    }
    return std::nullopt;
}

bool SmsSubsystem::openTransmitSocket()
{
    if (equalsIgnoreCase(m_config.outboundMode, "brandmeister") || equalsIgnoreCase(m_config.outboundMode, "bm")) {
        ::LogInfoEx(LOG_HOST, "SMS subsystem outbound mode is BrandMeister packet-data; UDP transmit socket disabled");
        return true;
    }

    if (m_config.outboundAddress == m_config.bindAddress &&
        m_config.outboundArsPort == m_config.arsPort &&
        m_config.outboundTmsPort == m_config.tmsPort) {
        ::LogWarning(LOG_HOST, "SMS subsystem outbound target matches local listener ports; transmit path disabled to avoid local loop");
        return true;
    }

    const auto* preferredTarget = m_tmsTarget.valid ? &m_tmsTarget : (m_arsTarget.valid ? &m_arsTarget : nullptr);
    if (preferredTarget == nullptr) {
        ::LogWarning(LOG_HOST, "SMS subsystem has no resolved outbound target; transmit path disabled");
        return true;
    }

    if (!m_txSocket.open(preferredTarget->storage)) {
        ::LogError(LOG_HOST, "SMS subsystem failed to open transmit socket");
        return false;
    }

    return true;
}

void SmsSubsystem::ensureDirectories() const
{
    std::error_code ec;
    std::filesystem::create_directories(m_config.inboxPath, ec);
    ec.clear();
    std::filesystem::create_directories(m_config.outboxPath, ec);
    ec.clear();
    std::filesystem::create_directories(m_config.sentPath, ec);
    ec.clear();
    std::filesystem::create_directories(m_config.p25OutboxPath, ec);
}

void SmsSubsystem::processIncomingSocket(Socket& socket, const char* channelName)
{
    for (uint32_t packetCount = 0U; packetCount < kMaxInboundPacketsPerClock; ++packetCount) {
        sockaddr_storage source {};
        uint32_t sourceLength = 0U;
        const int length = socket.read(m_rxBuffer.data(), static_cast<uint32_t>(m_rxBuffer.size()), source, sourceLength);
        if (length <= 0) {
            return;
        }

        handleIncomingPacket(channelName, m_rxBuffer.data(), static_cast<uint32_t>(length), source, sourceLength);
    }
}

void SmsSubsystem::handleIncomingPacket(const char* channelName, const uint8_t* data, uint32_t length, const sockaddr_storage& source, uint32_t sourceLength)
{
    if (const auto parsed = parseMotorolaPacket(channelName, data, length); parsed.has_value()) {
        m_registeredSubscribers[parsed->sourceRid] = std::chrono::steady_clock::now();
        const std::string& parsedText = parsed->text.empty() ? parsed->textFragment : parsed->text;
        ::LogInfoEx(LOG_HOST, "SMS inbound %s packet parsed, app=%s srcRid=%u targetRid=%u textLength=%u",
            channelName, parsed->application.c_str(), parsed->sourceRid, parsed->targetRid,
            static_cast<uint32_t>(parsedText.size()));
    }

    const std::string stem = makeEventStem(channelName);
    const std::string jsonText = buildInboundJson(channelName, data, length, source, sourceLength);
    if (writeInboundEvent(stem, jsonText)) {
        ::LogInfoEx(LOG_HOST, "SMS inbound %s packet captured, len=%u, source=%s",
            channelName, length, sockaddrToString(source, sourceLength).c_str());
    }
}

void SmsSubsystem::processOutboundQueue()
{
    std::vector<std::filesystem::path> files;
    std::error_code ec;
    for (const auto& entry : std::filesystem::directory_iterator(m_config.outboxPath, ec)) {
        if (ec) {
            ::LogWarning(LOG_HOST, "SMS outbox scan failed: %s", ec.message().c_str());
            return;
        }

        if (entry.is_regular_file()) {
            files.push_back(entry.path());
        }
    }

    std::sort(files.begin(), files.end());

    if (files.size() > kMaxOutboundFilesPerPoll) {
        files.resize(kMaxOutboundFilesPerPoll);
    }

    for (const auto& path : files) {
        processOutboundFile(path);
    }
}

bool SmsSubsystem::processOutboundFile(const std::filesystem::path& path)
{
    YAML::Node root;
    try {
        root = YAML::LoadFile(path.string());
    } catch (const std::exception& ex) {
        ::LogError(LOG_HOST, "SMS outbox file %s could not be parsed: %s", path.string().c_str(), ex.what());
        return false;
    }

    const std::string channel = root["channel"] ? root["channel"].as<std::string>() : "tms";
    const bool useTms = channel != "ars";
    const bool useLrrp = equalsIgnoreCase(channel, "lrrp") || equalsIgnoreCase(channel, "location");
    const std::string route = root["route"] ? root["route"].as<std::string>() : m_config.outboundMode;
    const bool useBrandmeister = equalsIgnoreCase(route, "brandmeister") || equalsIgnoreCase(route, "bm");
    const TargetAddress& target = useTms ? m_tmsTarget : m_arsTarget;
    const uint32_t sourceRid = root["sourceRid"] ? root["sourceRid"].as<uint32_t>() : 0U;
    const uint32_t targetRid = root["targetRid"] ? root["targetRid"].as<uint32_t>() : 0U;
    const bool hasRidPair = sourceRid != 0U && targetRid != 0U;

    std::optional<std::vector<uint8_t>> rawIpPacket;
    YAML::Node rawIpPacketNode = root["rawIpPacketHex"];
    if (!rawIpPacketNode) {
        rawIpPacketNode = root["hexIpPacket"];
    }
    if (rawIpPacketNode) {
        rawIpPacket.emplace();
        if (!hexToBytes(rawIpPacketNode.as<std::string>(), rawIpPacket.value())) {
            ::LogError(LOG_HOST, "SMS outbox file %s contains invalid rawIpPacketHex", path.string().c_str());
            return false;
        }
    }

    std::vector<uint8_t> payload;
    std::optional<std::vector<uint8_t>> arsPayload;
    if (rawIpPacket.has_value()) {
        // The file already contains a complete IPv4/UDP Motorola packet captured from RF.
    } else if (root["hexPayload"]) {
        if (!hexToBytes(root["hexPayload"].as<std::string>(), payload)) {
            ::LogError(LOG_HOST, "SMS outbox file %s contains invalid hexPayload", path.string().c_str());
            return false;
        }
    } else if (hasRidPair && (!useTms || root["text"])) {
        if (useTms) {
            if (root["sendArsFirst"] && root["sendArsFirst"].as<bool>()) {
                arsPayload = buildMotorolaArsPayload(sourceRid, targetRid);
            }
            const uint8_t messageId = m_nextTmsMessageId & 0x1FU;
            payload = buildMotorolaTmsPayload(sourceRid, root["text"].as<std::string>(),
                messageId, useBrandmeister);
            m_nextTmsMessageId = static_cast<uint8_t>((messageId + 1U) & 0x1FU);
        } else {
            payload = buildMotorolaArsPayload(sourceRid, targetRid);
        }
    } else if (!useBrandmeister && root["text"]) {
        payload = encodeUtf16Le(root["text"].as<std::string>(), root["appendNullTerminator"] ?
            root["appendNullTerminator"].as<bool>() : m_config.outboundAppendNullTerminator);
    } else {
        ::LogError(LOG_HOST, "SMS outbox file %s has neither routable text nor hexPayload", path.string().c_str());
        return false;
    }

    if (!rawIpPacket.has_value() && payload.empty()) {
        ::LogWarning(LOG_HOST, "SMS outbox file %s produced an empty payload", path.string().c_str());
        return false;
    }

    auto archiveSent = [&]() {
        const auto archivedPath = std::filesystem::path(m_config.sentPath) / (path.filename().string() + ".sent");
        std::error_code ec;
        std::filesystem::rename(path, archivedPath, ec);
        if (ec) {
            ec.clear();
            std::filesystem::copy_file(path, archivedPath, std::filesystem::copy_options::overwrite_existing, ec);
            if (!ec) {
                ec.clear();
                std::filesystem::remove(path, ec);
            }
        }

        return true;
    };

    if (useBrandmeister) {
        if (!hasRidPair) {
            ::LogError(LOG_HOST, "SMS outbox file %s cannot use BrandMeister route without sourceRid and targetRid", path.string().c_str());
            return false;
        }
        if (!m_bmPacketDataWriter) {
            ::LogWarning(LOG_HOST, "SMS outbox file %s cannot use BrandMeister route because no packet-data writer is installed", path.string().c_str());
            return false;
        }

        const uint8_t slot = static_cast<uint8_t>(root["bmSlot"] ? root["bmSlot"].as<uint16_t>() : m_config.bmSlot);

        auto resolveBmIp = [&](const char* key, const std::string& configured, uint32_t rid) {
            const std::string value = root[key] ? root[key].as<std::string>() : configured;
            if (!value.empty() && !equalsIgnoreCase(value, "auto")) {
                return value;
            }
            return motorolaRadioIp(rid);
        };
        const std::string sourceIp = resolveBmIp("bmSourceIp", m_config.bmSourceIp, sourceRid);
        const std::string targetIp = resolveBmIp("bmTargetIp", m_config.bmTargetIp, targetRid);
        const uint16_t arsSourcePort = root["arsUdpSrcPort"] ? root["arsUdpSrcPort"].as<uint16_t>() : m_config.outboundArsPort;
        const uint16_t arsTargetPort = root["arsUdpDstPort"] ? root["arsUdpDstPort"].as<uint16_t>() : m_config.outboundArsPort;
        const uint16_t tmsSourcePort = root["udpSrcPort"] ? root["udpSrcPort"].as<uint16_t>() : m_config.outboundTmsPort;
        const uint16_t tmsTargetPort = root["udpDstPort"] ? root["udpDstPort"].as<uint16_t>() : m_config.outboundTmsPort;

        auto sendBmPacket = [&](const std::vector<uint8_t>& udpPayload, uint16_t sourcePort, uint16_t targetPort, const char* label) {
            const std::vector<uint8_t> ipPacket = buildIpv4UdpPacket(sourceIp, targetIp, sourcePort, targetPort, udpPayload);
            if (ipPacket.empty()) {
                ::LogWarning(LOG_HOST, "SMS outbound BM %s packet build failed for %s", label, path.string().c_str());
                return false;
            }
            if (!m_bmPacketDataWriter(sourceRid, targetRid, slot, ipPacket)) {
                ::LogWarning(LOG_HOST, "SMS outbound BM %s send failed for %s", label, path.string().c_str());
                return false;
            }
            return true;
        };

        if (rawIpPacket.has_value()) {
            if (!useLrrp) {
                archiveSent();
                ::LogWarning(LOG_HOST, "SMS outbound BM raw-IP packet-data dropped for %s; BrandMeister SMS uses normalized text events",
                    path.string().c_str());
                return true;
            }

            std::vector<uint8_t> lrrpPayload;
            bool hasLrrpPosition = false;
            if (!extractMotorolaLrrpPayload(rawIpPacket.value(), lrrpPayload,
                    hasLrrpPosition)) {
                ::LogError(LOG_HOST, "SMS outbound BM LRRP packet in %s is not a valid Motorola location report",
                    path.string().c_str());
                return false;
            }
            if (!hasLrrpPosition) {
                archiveSent();
                ::LogWarning(LOG_HOST, "SMS outbound BM LRRP status without coordinates dropped for %s",
                    path.string().c_str());
                return true;
            }
            if (!sendBmPacket(lrrpPayload, 4001U, 4001U, "LRRP")) {
                return false;
            }

            archiveSent();
            ::LogInfoEx(LOG_HOST,
                "SMS outbound BM LRRP packet-data sent, srcRid=%u dstRid=%u sourceIp=%s targetIp=%s len=%u file=%s",
                sourceRid, targetRid, sourceIp.c_str(), targetIp.c_str(),
                static_cast<uint32_t>(lrrpPayload.size()), path.string().c_str());
            return true;
        }

        if (arsPayload.has_value() && !sendBmPacket(arsPayload.value(), arsSourcePort, arsTargetPort, "ARS")) {
            return false;
        }

        if (!sendBmPacket(payload, useTms ? tmsSourcePort : arsSourcePort, useTms ? tmsTargetPort : arsTargetPort, useTms ? "TMS" : "ARS")) {
            return false;
        }

        archiveSent();
        ::LogInfoEx(LOG_HOST,
            "SMS outbound BM %s packet-data sent, srcRid=%u dstRid=%u sourceIp=%s targetIp=%s len=%u file=%s",
            useTms ? "TMS" : "ARS", sourceRid, targetRid, sourceIp.c_str(), targetIp.c_str(),
            static_cast<uint32_t>(payload.size()), path.string().c_str());
        return true;
    }

    if (!target.valid) {
        ::LogWarning(LOG_HOST, "SMS outbound file %s skipped because %s target is unresolved",
            path.string().c_str(), useTms ? "TMS" : "ARS");
        return false;
    }

    if (arsPayload.has_value() && m_arsTarget.valid) {
        m_txSocket.write(arsPayload->data(), static_cast<uint32_t>(arsPayload->size()), m_arsTarget.storage, m_arsTarget.length);
    }

    if (!m_txSocket.write(payload.data(), static_cast<uint32_t>(payload.size()), target.storage, target.length)) {
        ::LogWarning(LOG_HOST, "SMS outbound send failed for %s (%s)", path.string().c_str(), useTms ? "TMS" : "ARS");
        return false;
    }

    archiveSent();
    ::LogInfoEx(LOG_HOST, "SMS outbound UDP %s packet sent, len=%u, file=%s",
        useTms ? "TMS" : "ARS", static_cast<uint32_t>(payload.size()), path.string().c_str());
    return true;
}

bool SmsSubsystem::writeInboundEvent(const std::string& fileNameStem, const std::string& jsonText) const
{
    const auto targetPath = std::filesystem::path(m_config.inboxPath) / (fileNameStem + ".json");
    const auto tempPath = targetPath.string() + ".tmp";

    std::ofstream output(tempPath, std::ios::trunc | std::ios::binary);
    if (!output.is_open()) {
        ::LogWarning(LOG_HOST, "SMS subsystem failed to open %s for writing", tempPath.c_str());
        return false;
    }

    output << jsonText;
    output.close();
    if (!output) {
        ::LogWarning(LOG_HOST, "SMS subsystem failed while writing %s", tempPath.c_str());
        return false;
    }

    std::error_code ec;
    std::filesystem::rename(tempPath, targetPath, ec);
    if (ec) {
        ::LogWarning(LOG_HOST, "SMS subsystem failed to publish inbound event %s: %s", targetPath.string().c_str(), ec.message().c_str());
        std::filesystem::remove(tempPath, ec);
        return false;
    }

    return true;
}

std::string SmsSubsystem::makeEventStem(const char* prefix)
{
    const auto now = std::chrono::system_clock::now().time_since_epoch();
    const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(now).count();
    std::ostringstream stream;
    stream << prefix << '-' << millis << '-' << std::setw(6) << std::setfill('0') << (m_eventCounter % 1000000ULL);
    ++m_eventCounter;
    return stream.str();
}

std::string SmsSubsystem::sockaddrToString(const sockaddr_storage& address, uint32_t) const
{
#if defined(_WIN32)
    (void)address;
    return "unknown";
#else
    char host[INET6_ADDRSTRLEN] {};
    uint16_t port = 0U;

    if (address.ss_family == AF_INET) {
        const auto* addr4 = reinterpret_cast<const sockaddr_in*>(&address);
        if (::inet_ntop(AF_INET, &addr4->sin_addr, host, sizeof(host)) != nullptr) {
            port = ntohs(addr4->sin_port);
        }
    } else if (address.ss_family == AF_INET6) {
        const auto* addr6 = reinterpret_cast<const sockaddr_in6*>(&address);
        if (::inet_ntop(AF_INET6, &addr6->sin6_addr, host, sizeof(host)) != nullptr) {
            port = ntohs(addr6->sin6_port);
        }
    }

    std::ostringstream stream;
    if (host[0] == '\0') {
        stream << "unknown";
    } else {
        stream << host;
    }
    if (port != 0U) {
        stream << ':' << port;
    }
    return stream.str();
#endif
}

std::string SmsSubsystem::buildInboundJson(const char* channelName, const uint8_t* data, uint32_t length, const sockaddr_storage& source, uint32_t sourceLength) const
{
    const auto now = std::chrono::system_clock::now().time_since_epoch();
    const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(now).count();
    const std::string text = (m_config.decodeUtf16Le && std::strcmp(channelName, "tms") == 0) ? extractText(data, length) : std::string();
    const auto parsed = parseMotorolaPacket(channelName, data, length);

    std::ostringstream json;
    json << "{\n";
    json << "  \"direction\": \"inbound\",\n";
    json << "  \"channel\": \"" << jsonEscape(channelName) << "\",\n";
    json << "  \"timestampMs\": " << millis << ",\n";
    json << "  \"source\": \"" << jsonEscape(sockaddrToString(source, sourceLength)) << "\",\n";
    json << "  \"length\": " << length << ",\n";
    json << "  \"hexPayload\": \"" << bytesToHex(data, length) << "\"";
    if (parsed.has_value()) {
        json << ",\n  \"application\": \"" << jsonEscape(parsed->application) << "\"";
        json << ",\n  \"sourceRid\": " << parsed->sourceRid;
        json << ",\n  \"targetRid\": " << parsed->targetRid;
        json << ",\n  \"broadcast\": " << (parsed->broadcast ? "true" : "false");
        json << ",\n  \"localDeliveryCandidate\": " << (m_registeredSubscribers.count(parsed->targetRid) > 0U ? "true" : "false");
        if (!parsed->text.empty()) {
            json << ",\n  \"parsedText\": \"" << jsonEscape(parsed->text) << "\"";
        } else if (!parsed->textFragment.empty()) {
            json << ",\n  \"parsedTextFragment\": \"" << jsonEscape(parsed->textFragment) << "\"";
        }
    }
    if (!text.empty()) {
        json << ",\n  \"text\": \"" << jsonEscape(text) << "\"";
    }
    json << "\n}\n";
    return json.str();
}

std::string SmsSubsystem::extractText(const uint8_t* data, uint32_t length) const
{
    if (length < 4U) {
        return {};
    }

    std::u16string current;
    std::u16string best;

    for (uint32_t offset = 0U; offset + 1U < length; offset += 2U) {
        const uint16_t value = static_cast<uint16_t>(data[offset]) |
            (static_cast<uint16_t>(data[offset + 1U]) << 8U);

        if (value == 0x0000U) {
            if (current.size() > best.size()) {
                best = current;
            }
            current.clear();
            continue;
        }

        if (isPrintableUtf16(value)) {
            current.push_back(static_cast<char16_t>(value));
            continue;
        }

        if (current.size() > best.size()) {
            best = current;
        }
        current.clear();
    }

    if (current.size() > best.size()) {
        best = current;
    }

    if (best.size() < 3U) {
        return {};
    }

    std::wstring_convert<std::codecvt_utf8_utf16<char16_t>, char16_t> convert;
    return trim(convert.to_bytes(best));
}

std::optional<SmsSubsystem::ParsedSmsPacket> SmsSubsystem::parseMotorolaArs(const uint8_t* data, uint32_t length)
{
    if (data == nullptr || length < 19U) {
        return std::nullopt;
    }
    static constexpr uint8_t kPrefix[] = {0x00U, 0x11U, 0x72U, 0x07U};
    if (::memcmp(data, kPrefix, sizeof(kPrefix)) != 0) {
        return std::nullopt;
    }

    std::string src(reinterpret_cast<const char*>(data + 4U), 7U);
    std::string dst(reinterpret_cast<const char*>(data + 12U), 7U);
    if (src.find_first_not_of("0123456789") != std::string::npos || dst.find_first_not_of("0123456789") != std::string::npos) {
        return std::nullopt;
    }

    ParsedSmsPacket packet;
    packet.application = "motorola_ars";
    packet.sourceRid = static_cast<uint32_t>(std::stoul(src));
    packet.targetRid = static_cast<uint32_t>(std::stoul(dst));
    packet.broadcast = packet.targetRid == 16777215U;
    return packet;
}

std::string SmsSubsystem::extractTmsTextFragment(const uint8_t* data, uint32_t length, size_t markerOffset)
{
    std::string out;
    for (size_t i = markerOffset + 2U; i < length;) {
        if (i + 1U < length && data[i] == 0x00U && i + 2U < length && data[i + 2U] == 0x00U &&
            data[i + 1U] >= 0x20U && data[i + 1U] <= 0x7EU) {
            out.push_back(static_cast<char>(data[i + 1U]));
            i += 3U;
            continue;
        }
        if (i + 1U < length && data[i + 1U] == 0x00U && data[i] >= 0x20U && data[i] <= 0x7EU) {
            out.push_back(static_cast<char>(data[i]));
            i += 2U;
            continue;
        }
        if (data[i] >= 0x20U && data[i] <= 0x7EU) {
            out.push_back(static_cast<char>(data[i]));
        }
        break;
    }
    return trim(out);
}

std::optional<SmsSubsystem::ParsedSmsPacket> SmsSubsystem::parseMotorolaTms(const uint8_t* data, uint32_t length)
{
    if (data == nullptr || length < 12U) {
        return std::nullopt;
    }

    size_t markerOffset = std::string::npos;
    for (size_t i = 0U; i + 1U < length; ++i) {
        if (data[i] == 0x80U && data[i + 1U] == 0x04U) {
            markerOffset = i;
            break;
        }
    }
    if (markerOffset == std::string::npos) {
        // BrandMeister's server-originated E0 messages use a message-id/encoding
        // pair followed by CRLF and UTF-16LE text instead of the 80 04 marker.
        if (length < 10U || data[2U] != 0xE0U) {
            return std::nullopt;
        }

        size_t textOffset = std::string::npos;
        for (size_t i = 4U; i + 3U < length; i += 2U) {
            if (data[i] == 0x0DU && data[i + 1U] == 0x00U &&
                data[i + 2U] == 0x0AU && data[i + 3U] == 0x00U) {
                textOffset = i + 4U;
                break;
            }
        }
        if (textOffset == std::string::npos) {
            return std::nullopt;
        }

        std::u16string decoded;
        for (size_t i = textOffset; i + 1U < length; i += 2U) {
            const uint16_t value = static_cast<uint16_t>(data[i]) |
                (static_cast<uint16_t>(data[i + 1U]) << 8U);
            if (value == 0x0000U) {
                break;
            }
            if (!isPrintableUtf16(value)) {
                return std::nullopt;
            }
            decoded.push_back(static_cast<char16_t>(value));
        }
        if (decoded.empty()) {
            return std::nullopt;
        }

        ParsedSmsPacket packet;
        packet.application = "motorola_tms";
        packet.operation = data[2U];
        packet.messageId = data[4U] & 0x1FU;
        std::wstring_convert<std::codecvt_utf8_utf16<char16_t>, char16_t> convert;
        packet.text = trim(convert.to_bytes(decoded));
        return packet.text.empty() ? std::nullopt : std::optional<ParsedSmsPacket>(std::move(packet));
    }

    ParsedSmsPacket packet;
    packet.application = "motorola_tms";
    packet.operation = data[2U];
    packet.messageId = data[markerOffset] & 0x1FU;
    packet.textFragment = extractTmsTextFragment(data, length, markerOffset);

    std::string digits;
    for (size_t i = 0U; i + 1U < markerOffset; i += 2U) {
        if (data[i + 1U] == 0x00U && data[i] >= '0' && data[i] <= '9') {
            digits.push_back(static_cast<char>(data[i]));
        }
    }
    if (digits.size() >= 7U) {
        packet.sourceRid = static_cast<uint32_t>(std::stoul(digits.substr(0U, 7U)));
    }
    return packet;
}

std::vector<uint8_t> SmsSubsystem::buildMotorolaArsPayload(uint32_t sourceRid, uint32_t targetRid)
{
    std::ostringstream src;
    src << std::setw(7) << std::setfill('0') << sourceRid;
    std::ostringstream dst;
    dst << std::setw(7) << std::setfill('0') << targetRid;

    std::vector<uint8_t> payload = {0x00U, 0x11U, 0x72U, 0x07U};
    const std::string srcText = src.str();
    payload.insert(payload.end(), srcText.begin(), srcText.end());
    payload.push_back(0x07U);
    const std::string dstText = dst.str();
    payload.insert(payload.end(), dstText.begin(), dstText.end());
    return payload;
}

std::vector<uint8_t> SmsSubsystem::buildMotorolaTmsPayload(uint32_t sourceRid,
    const std::string& text, uint8_t messageId, bool brandmeisterFormat)
{
    const std::vector<uint8_t> address = brandmeisterFormat ?
        std::vector<uint8_t>() : encodeUtf16Le(std::to_string(sourceRid), false);
    if (address.size() > 0xFFU) {
        return {};
    }

    std::vector<uint8_t> payload = {
        0x00U, 0x00U, 0xE0U, static_cast<uint8_t>(address.size())
    };
    payload.insert(payload.end(), address.begin(), address.end());
    payload.push_back(static_cast<uint8_t>(0x80U | (messageId & 0x1FU)));
    payload.push_back(0x04U);

    const std::string body = brandmeisterFormat ? "\r\n" + text : text;
    const std::vector<uint8_t> textBytes = encodeUtf16Le(body, false);
    payload.insert(payload.end(), textBytes.begin(), textBytes.end());
    if (payload.size() - 2U > 0xFFFFU) {
        return {};
    }
    writeUint16(payload, 0U, static_cast<uint16_t>(payload.size() - 2U));
    return payload;
}

std::vector<uint8_t> SmsSubsystem::buildIpv4UdpPacket(const std::string& sourceIp, const std::string& targetIp,
    uint16_t sourcePort, uint16_t targetPort, const std::vector<uint8_t>& payload)
{
    std::array<uint8_t, 4U> source {};
    std::array<uint8_t, 4U> target {};
    if (!parseIpv4Address(sourceIp, source) || !parseIpv4Address(targetIp, target)) {
        ::LogWarning(LOG_HOST, "SMS outbound BM packet has invalid IPv4 addresses source=%s target=%s",
            sourceIp.c_str(), targetIp.c_str());
        return {};
    }

    const size_t udpLength = 8U + payload.size();
    const size_t totalLength = 20U + udpLength;
    if (totalLength > 0xFFFFU) {
        ::LogWarning(LOG_HOST, "SMS outbound BM packet is too large for IPv4, len=%u", static_cast<uint32_t>(totalLength));
        return {};
    }

    std::vector<uint8_t> packet(totalLength, 0x00U);
    packet[0U] = 0x45U;
    packet[1U] = 0x00U;
    writeUint16(packet, 2U, static_cast<uint16_t>(totalLength));
    writeUint16(packet, 4U, 0U);
    writeUint16(packet, 6U, 0U);
    packet[8U] = 64U;
    packet[9U] = 17U;
    std::copy(source.begin(), source.end(), packet.begin() + 12U);
    std::copy(target.begin(), target.end(), packet.begin() + 16U);

    const uint16_t ipChecksum = checksumFinalize(checksumAccumulate(packet.data(), 20U));
    writeUint16(packet, 10U, ipChecksum);

    writeUint16(packet, 20U, sourcePort);
    writeUint16(packet, 22U, targetPort);
    writeUint16(packet, 24U, static_cast<uint16_t>(udpLength));
    std::copy(payload.begin(), payload.end(), packet.begin() + 28U);

    uint32_t udpSum = 0U;
    udpSum = checksumAccumulate(source.data(), source.size(), udpSum);
    udpSum = checksumAccumulate(target.data(), target.size(), udpSum);
    const uint8_t pseudo[] = {0x00U, 17U, static_cast<uint8_t>(udpLength >> 8U), static_cast<uint8_t>(udpLength)};
    udpSum = checksumAccumulate(pseudo, sizeof(pseudo), udpSum);
    udpSum = checksumAccumulate(packet.data() + 20U, udpLength, udpSum);
    uint16_t udpChecksum = checksumFinalize(udpSum);
    if (udpChecksum == 0U) {
        udpChecksum = 0xFFFFU;
    }
    writeUint16(packet, 26U, udpChecksum);

    return packet;
}

std::string SmsSubsystem::bytesToHex(const uint8_t* data, uint32_t length)
{
    std::ostringstream stream;
    stream << std::hex << std::setfill('0');
    for (uint32_t i = 0U; i < length; ++i) {
        stream << std::setw(2) << static_cast<unsigned>(data[i]);
    }
    return stream.str();
}

bool SmsSubsystem::hexToBytes(const std::string& hex, std::vector<uint8_t>& output)
{
    auto nibble = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
        if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
        return -1;
    };

    std::string clean;
    clean.reserve(hex.size());
    for (char c : hex) {
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') {
            clean.push_back(c);
        }
    }

    if ((clean.size() % 2U) != 0U) {
        return false;
    }

    output.clear();
    output.reserve(clean.size() / 2U);
    for (size_t i = 0U; i < clean.size(); i += 2U) {
        const int hi = nibble(clean[i]);
        const int lo = nibble(clean[i + 1U]);
        if (hi < 0 || lo < 0) {
            return false;
        }
        output.push_back(static_cast<uint8_t>((hi << 4U) | lo));
    }

    return true;
}

std::string SmsSubsystem::jsonEscape(const std::string& value)
{
    std::ostringstream stream;
    for (unsigned char c : value) {
        switch (c) {
        case '\\':
            stream << "\\\\";
            break;
        case '"':
            stream << "\\\"";
            break;
        case '\b':
            stream << "\\b";
            break;
        case '\f':
            stream << "\\f";
            break;
        case '\n':
            stream << "\\n";
            break;
        case '\r':
            stream << "\\r";
            break;
        case '\t':
            stream << "\\t";
            break;
        default:
            if (c < 0x20U) {
                stream << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<unsigned>(c) << std::dec;
            } else {
                stream << static_cast<char>(c);
            }
            break;
        }
    }
    return stream.str();
}

std::vector<uint8_t> SmsSubsystem::encodeUtf16Le(const std::string& text, bool appendNullTerminator)
{
    std::wstring_convert<std::codecvt_utf8_utf16<char16_t>, char16_t> convert;
    const std::u16string wide = convert.from_bytes(text);

    std::vector<uint8_t> output;
    output.reserve((wide.size() + (appendNullTerminator ? 1U : 0U)) * 2U);

    for (char16_t codeUnit : wide) {
        output.push_back(static_cast<uint8_t>(codeUnit & 0x00FFU));
        output.push_back(static_cast<uint8_t>((codeUnit >> 8U) & 0x00FFU));
    }

    if (appendNullTerminator) {
        output.push_back(0x00U);
        output.push_back(0x00U);
    }

    return output;
}
