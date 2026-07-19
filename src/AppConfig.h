#pragma once

#include <cstdint>
#include <string>
#include <vector>
#include <unordered_set>
#include <unordered_map>

struct SmsConfig {
    bool enabled {false};
    std::string bindAddress {"127.0.0.1"};
    uint16_t arsPort {4005U};
    uint16_t tmsPort {4007U};
    std::string outboundAddress {"127.0.0.1"};
    uint16_t outboundArsPort {4005U};
    uint16_t outboundTmsPort {4007U};
    std::string outboundMode {"udp"};
    std::string bmSourceIp {"auto"};
    std::string bmTargetIp {"auto"};
    uint16_t bmSlot {2U};
    std::string inboxPath {"./sms/inbox"};
    std::string outboxPath {"./sms/outbox"};
    std::string sentPath {"./sms/sent"};
    std::string p25OutboxPath {"./sms/p25-outbox"};
    std::string serviceRoutePath {"./sms/service-routes"};
    uint32_t pollIntervalMs {100U};
    uint32_t maxPacketBytes {2048U};
    bool decodeUtf16Le {true};
    bool outboundAppendNullTerminator {false};
};

struct FNEConfig {
    uint32_t peerId {9000101U};
    std::string address {"127.0.0.1"};
    uint16_t port {62031U};
    uint16_t localPort {0U};
    std::string password;
    bool encrypted {false};
    std::string presharedKey;
    bool debug {false};
};

struct BMConfig {
    uint32_t repeaterId {123456U};
    std::string password;
    std::string transport {"brandmeister"};
    bool voiceEnabled {true};
    std::string address {"2622.master.brandmeister.network"};
    uint16_t port {62031U};
    uint16_t localPort {62032U};
    std::string callsign {"N0CALL"};
    uint32_t rxFrequency {438800000U};
    uint32_t txFrequency {438800000U};
    uint32_t power {25U};
    uint32_t colorCode {1U};
    std::string latitude {"0.0000"};
    std::string longitude {"0.0000"};
    std::string height {"0"};
    std::string location {"Example Site"};
    std::string description {"Quantar P25 BM Link"};
    std::string url;
    std::string softwareId {"20260320"};
    std::string packageId {"MMDVM_HBlink"};
    uint8_t timeslot {2U};
    bool slot1 {false};
    bool slot2 {true};
    std::string options;
    bool debug {false};
};

struct RoutingConfig {
    uint32_t dynamicTimeoutSeconds {10U};
    uint32_t rfPriorityHoldoffSeconds {8U};
    uint32_t disconnectTalkgroup {4000U};
    bool allowPrivateCalls {false};
    std::vector<uint32_t> staticTalkgroupOrder;
    std::unordered_set<uint32_t> staticTalkgroups;
    std::unordered_map<uint32_t, uint32_t> p25ToBmTalkgroups;
    std::unordered_map<uint32_t, uint32_t> bmToP25Talkgroups;
};

struct LoggingConfig {
    std::string filePath {"."};
    std::string fileRoot {"quantarbridge"};
    uint32_t fileLevel {2U};
    uint32_t displayLevel {2U};
    bool useSyslog {false};
};

struct AppConfig {
    LoggingConfig logging;
    FNEConfig fne;
    BMConfig bm;
    RoutingConfig routing;
    SmsConfig sms;
};

AppConfig loadConfig(const std::string& path);
