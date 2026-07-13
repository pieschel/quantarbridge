#pragma once

#include "AppConfig.h"

#include "common/Timer.h"
#include "common/dmr/data/NetData.h"
#include "common/network/udp/Socket.h"

#include <cstdint>
#include <deque>
#include <random>
#include <string>
#include <unordered_map>
#include <vector>

class BMNetwork {
public:
    explicit BMNetwork(const BMConfig& config);

    bool open();
    void close();
    void clock(uint32_t ms);

    bool isConnected() const;
    bool read(dmr::data::NetData& data);
    bool write(const dmr::data::NetData& data);
    bool writePacketData(uint32_t sourceRid, uint32_t targetRid, uint8_t slotNo, const std::vector<uint8_t>& ipPacket);

private:
    enum class Status : uint8_t {
        WaitingConnect,
        WaitingLogin,
        WaitingAuthorisation,
        WaitingConfig,
        WaitingOptions,
        Running
    };

    bool writeLogin();
    bool writeAuthorisation();
    bool writeConfig();
    bool writeOptions();
    bool writePing();
    bool writeGatewayPong();
    bool writeGatewayBeacon();
    bool writePacket(const uint8_t* data, uint32_t length);
    void deferReconnect();
    std::vector<uint8_t> buildHomebrewPacket(const dmr::data::NetData& data, uint32_t streamId) const;
    void clockPacketData(uint32_t ms);
    void parseIncomingDMR(const uint8_t* buffer, uint32_t length);
    bool isGatewayMode() const;
    uint32_t nextStreamId();

    BMConfig m_config;
    network::udp::Socket m_socket;
    sockaddr_storage m_address {};
    uint32_t m_addressLen {0U};
    bool m_socketActive {false};
    Status m_status {Status::WaitingConnect};
    std::deque<std::vector<uint8_t>> m_rxPackets;
    Timer m_retryTimer;
    Timer m_timeoutTimer;
    Timer m_beaconTimer;
    std::mt19937 m_rng;
    std::unordered_map<uint32_t, uint8_t> m_packetDataNs;
    std::deque<std::vector<uint8_t>> m_packetDataTxQueue;
    uint32_t m_packetDataTxElapsedMs {0U};
    uint8_t m_repeaterId[4U] {};
    uint8_t m_netId[4U] {};
    uint8_t m_salt[4U] {};
    uint32_t m_streamId[2U] {};
    uint8_t m_buffer[512U] {};
};
