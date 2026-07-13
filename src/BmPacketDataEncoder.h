#pragma once

#include "common/dmr/data/NetData.h"

#include <cstdint>
#include <vector>

class BmPacketDataEncoder {
public:
    static std::vector<dmr::data::NetData> encode(uint32_t sourceRid, uint32_t targetRid,
        uint8_t slotNo, uint8_t colorCode, uint8_t sequenceNo,
        const std::vector<uint8_t>& ipPacket, uint8_t preambleCount = 16U);
};
