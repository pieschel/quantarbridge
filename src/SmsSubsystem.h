#pragma once

#include "AppConfig.h"

#include "common/network/udp/Socket.h"

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

class SmsSubsystem {
public:
    explicit SmsSubsystem(const SmsConfig& config);

    bool open();
    void close();
    void clock();
    bool isEnabled() const;
    void setBmPacketDataWriter(std::function<bool(uint32_t, uint32_t, uint8_t, const std::vector<uint8_t>&)> writer);
    bool handleBrandmeisterPacketData(uint32_t sourceRid, uint32_t targetRid, uint8_t slotNo,
        const std::vector<uint8_t>& ipv4Packet);

private:
    struct ParsedSmsPacket {
        std::string application;
        uint32_t sourceRid {0U};
        uint32_t targetRid {0U};
        std::string text;
        std::string textFragment;
        bool broadcast {false};
        uint8_t operation {0U};
        uint8_t messageId {0U};
    };

    struct TargetAddress {
        sockaddr_storage storage {};
        uint32_t length {0U};
        bool valid {false};
    };

    struct ServiceReplyRoute {
        uint32_t requesterRid {0U};
        std::filesystem::path path;
    };

    bool openTransmitSocket();
    void ensureDirectories() const;
    void processIncomingSocket(network::udp::Socket& socket, const char* channelName);
    void handleIncomingPacket(const char* channelName, const uint8_t* data, uint32_t length, const sockaddr_storage& source, uint32_t sourceLength);
    void processOutboundQueue();
    bool processOutboundFile(const std::filesystem::path& path);
    bool writeInboundEvent(const std::string& fileNameStem, const std::string& jsonText) const;
    std::string makeEventStem(const char* prefix);
    std::string sockaddrToString(const sockaddr_storage& address, uint32_t addressLength) const;
    std::string buildInboundJson(const char* channelName, const uint8_t* data, uint32_t length, const sockaddr_storage& source, uint32_t sourceLength) const;
    std::string extractText(const uint8_t* data, uint32_t length) const;
    std::optional<ParsedSmsPacket> parseMotorolaPacket(const char* channelName, const uint8_t* data, uint32_t length) const;
    static std::optional<ParsedSmsPacket> parseMotorolaArs(const uint8_t* data, uint32_t length);
    static std::optional<ParsedSmsPacket> parseMotorolaTms(const uint8_t* data, uint32_t length);
    static std::string extractTmsTextFragment(const uint8_t* data, uint32_t length, size_t markerOffset);
    static std::vector<uint8_t> buildMotorolaArsPayload(uint32_t sourceRid, uint32_t targetRid);
    static std::vector<uint8_t> buildMotorolaTmsPayload(uint32_t sourceRid, const std::string& text,
        uint8_t messageId, bool brandmeisterFormat);
    bool sendBrandmeisterTmsAcknowledgement(uint32_t sourceRid, uint32_t targetRid, uint8_t slotNo,
        const std::vector<uint8_t>& ipv4Packet, uint16_t sourcePort, uint16_t targetPort,
        uint8_t requestOperation, uint8_t messageId);
    std::optional<ServiceReplyRoute> findServiceReplyRoute(uint32_t serviceRid) const;
    static std::vector<uint8_t> buildIpv4UdpPacket(const std::string& sourceIp, const std::string& targetIp,
        uint16_t sourcePort, uint16_t targetPort, const std::vector<uint8_t>& payload);
    static std::string bytesToHex(const uint8_t* data, uint32_t length);
    static bool hexToBytes(const std::string& hex, std::vector<uint8_t>& output);
    static std::string jsonEscape(const std::string& value);
    static std::vector<uint8_t> encodeUtf16Le(const std::string& text, bool appendNullTerminator);

    SmsConfig m_config;
    network::udp::Socket m_arsSocket;
    network::udp::Socket m_tmsSocket;
    network::udp::Socket m_txSocket;
    TargetAddress m_arsTarget;
    TargetAddress m_tmsTarget;
    std::function<bool(uint32_t, uint32_t, uint8_t, const std::vector<uint8_t>&)> m_bmPacketDataWriter;
    std::chrono::steady_clock::time_point m_nextOutboxPoll {};
    std::vector<uint8_t> m_rxBuffer;
    std::unordered_map<uint32_t, std::chrono::steady_clock::time_point> m_registeredSubscribers;
    std::unordered_map<std::string, std::chrono::steady_clock::time_point> m_recentBmTmsReplies;
    bool m_open {false};
    uint8_t m_nextTmsMessageId {0U};
    uint64_t m_eventCounter {0U};
};
