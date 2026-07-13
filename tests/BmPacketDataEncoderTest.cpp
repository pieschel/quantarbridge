#include "BmPacketDataEncoder.h"
#include "BmPacketDataReassembler.h"

#include "common/dmr/DMRDefines.h"
#include "common/dmr/SlotType.h"
#include "common/dmr/data/DataHeader.h"
#include "common/dmr/lc/csbk/CSBK_PRECCSBK.h"
#include "common/edac/BPTC19696.h"

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <vector>

using namespace dmr;
using namespace dmr::defines;

int main()
{
    std::vector<uint8_t> ipPacket(33U, 0x00U);
    ipPacket[0U] = 0x45U;
    ipPacket[2U] = 0x00U;
    ipPacket[3U] = static_cast<uint8_t>(ipPacket.size());
    ipPacket[8U] = 0x40U;
    ipPacket[9U] = 0x11U;
    for (size_t i = 20U; i < ipPacket.size(); ++i) {
        ipPacket[i] = static_cast<uint8_t>(i);
    }

    uint8_t directBurst[DMR_FRAME_LENGTH_BYTES] {};
    uint8_t directDecoded[DMR_PDU_HALFRATE_LENGTH_BYTES] {};
    edac::BPTC19696 directBptc;
    directBptc.encode(ipPacket.data(), directBurst);
    directBptc.decode(directBurst, directDecoded);
    if (!std::equal(directDecoded, directDecoded + sizeof(directDecoded), ipPacket.begin())) {
        std::cerr << "direct BPTC roundtrip failed\n";
        return 1;
    }

    constexpr uint32_t sourceRid = 1000002U;
    constexpr uint32_t targetRid = 1000001U;
    constexpr uint8_t preambleCount = 16U;
    const auto frames = BmPacketDataEncoder::encode(
        sourceRid, targetRid, 2U, 1U, 3U, ipPacket, preambleCount);
    if (frames.size() != 21U) {
        std::cerr << "unexpected encoded frame count: " << frames.size() << '\n';
        return 1;
    }

    for (size_t i = 0U; i < frames.size(); ++i) {
        const auto& frame = frames[i];
        if (frame.getSrcId() != sourceRid || frame.getDstId() != targetRid ||
            frame.getSlotNo() != 2U || frame.getFLCO() != FLCO::PRIVATE ||
            frame.getSeqNo() != static_cast<uint8_t>(i)) {
            std::cerr << "invalid frame routing metadata at index " << i << '\n';
            return 1;
        }

        uint8_t payload[DMR_FRAME_LENGTH_BYTES] {};
        frame.getData(payload);
        SlotType slotType;
        slotType.decode(payload);
        if (slotType.getColorCode() != 1U || slotType.getDataType() != frame.getDataType()) {
            std::cerr << "invalid slot metadata at index " << i << '\n';
            return 1;
        }

        if (i < preambleCount) {
            lc::csbk::CSBK_PRECCSBK preamble;
            if (!preamble.decode(payload) || !preamble.getDataContent() || preamble.getGI() ||
                preamble.getSrcId() != sourceRid || preamble.getDstId() != targetRid ||
                preamble.getCBF() != frames.size() - 1U - i) {
                std::cerr << "invalid preamble at index " << i << '\n';
                return 1;
            }
        }
    }

    uint8_t headerPayload[DMR_FRAME_LENGTH_BYTES] {};
    frames[preambleCount].getData(headerPayload);
    data::DataHeader header;
    if (!header.decode(headerPayload) || header.getDPF() != DPF::UNCONFIRMED_DATA ||
        header.getA() || header.getSAP() != PDUSAP::PACKET_DATA ||
        header.getBlocksToFollow() != 4U || header.getSrcId() != sourceRid ||
        header.getDstId() != targetRid) {
        std::cerr << "invalid packet-data header\n";
        return 1;
    }

    BmPacketDataReassembler reassembler;
    BmPacketDataReassembler::Result result;
    for (size_t i = preambleCount; i < frames.size(); ++i) {
        result = reassembler.push(frames[i]);
        if (!result.consumed) {
            std::cerr << "reassembler rejected frame " << i << '\n';
        }
    }
    if (!result.packet.has_value()) {
        std::cerr << "reassembler did not complete the packet\n";
        return 1;
    }
    if (result.packet->bytes != ipPacket) {
        std::cerr << "roundtrip length mismatch: expected " << ipPacket.size()
                  << ", got " << result.packet->bytes.size() << '\n';
        const size_t compareLength = std::min(ipPacket.size(), result.packet->bytes.size());
        for (size_t i = 0U; i < compareLength; ++i) {
            if (ipPacket[i] != result.packet->bytes[i]) {
                std::cerr << "first byte mismatch at " << i << ": expected "
                          << static_cast<uint32_t>(ipPacket[i]) << ", got "
                          << static_cast<uint32_t>(result.packet->bytes[i]) << '\n';
                break;
            }
        }
        std::cerr << "encoded packet did not survive a reassembly roundtrip\n";
        return 1;
    }

    return 0;
}
