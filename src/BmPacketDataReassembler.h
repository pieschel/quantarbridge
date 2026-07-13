#pragma once

#include "common/dmr/DMRDefines.h"
#include "common/dmr/data/DataHeader.h"
#include "common/dmr/data/NetData.h"

#include <chrono>
#include <cstdint>
#include <optional>
#include <unordered_map>
#include <vector>

class BmPacketDataReassembler {
public:
    struct Packet {
        uint32_t sourceRid {0U};
        uint32_t targetRid {0U};
        uint8_t slotNo {2U};
        uint8_t sequenceNo {0U};
        std::vector<uint8_t> bytes;
    };

    struct Result {
        bool consumed {false};
        std::optional<Packet> packet;
    };

    Result push(const dmr::data::NetData& frame,
        std::chrono::steady_clock::time_point now = std::chrono::steady_clock::now());

private:
    struct Session {
        dmr::data::DataHeader header;
        uint32_t expectedBlocks {0U};
        uint32_t receivedBlocks {0U};
        uint32_t nextUnconfirmedBlock {0U};
        bool confirmed {false};
        std::vector<std::vector<uint8_t>> blocks;
        std::vector<bool> received;
        std::chrono::steady_clock::time_point updatedAt {};
    };

    static uint64_t sessionKey(uint32_t sourceRid, uint32_t targetRid, uint8_t slotNo);
    static uint64_t packetSignature(const Packet& packet);
    void expire(std::chrono::steady_clock::time_point now);

    std::unordered_map<uint64_t, Session> m_sessions;
    std::unordered_map<uint64_t, std::chrono::steady_clock::time_point> m_recentPackets;
};
