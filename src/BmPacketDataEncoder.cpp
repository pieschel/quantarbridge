#include "BmPacketDataEncoder.h"

#include "common/dmr/DMRDefines.h"
#include "common/dmr/SlotType.h"
#include "common/dmr/Sync.h"
#include "common/dmr/data/Assembler.h"
#include "common/dmr/data/DataHeader.h"
#include "common/edac/BPTC19696.h"
#include "common/edac/CRC.h"

#include <algorithm>
#include <cstring>

using namespace dmr;
using namespace dmr::defines;

namespace {

constexpr DataType::E kPacketDataRate = DataType::RATE_12_DATA;

void addBurstMetadata(uint8_t* payload, DataType::E dataType, uint8_t colorCode)
{
    SlotType slotType;
    slotType.setColorCode(colorCode & 0x0FU);
    slotType.setDataType(dataType);
    slotType.encode(payload);
    Sync::addDMRDataSync(payload, true);
}

dmr::data::NetData makeFrame(uint32_t sourceRid, uint32_t targetRid, uint8_t slotNo,
    uint8_t sequenceNo, DataType::E dataType, const uint8_t* payload)
{
    dmr::data::NetData frame;
    frame.setSlotNo(slotNo);
    frame.setSrcId(sourceRid);
    frame.setDstId(targetRid);
    frame.setFLCO(FLCO::PRIVATE);
    frame.setN(0U);
    frame.setSeqNo(sequenceNo);
    frame.setDataType(dataType);
    frame.setData(payload);
    return frame;
}

dmr::data::NetData makePreamble(uint32_t sourceRid, uint32_t targetRid, uint8_t slotNo,
    uint8_t colorCode, uint8_t sequenceNo, uint8_t blocksToFollow)
{
    uint8_t csbk[DMR_CSBK_LENGTH_BYTES];
    ::memset(csbk, 0x00U, sizeof(csbk));
    csbk[0U] = static_cast<uint8_t>(0x80U | CSBKO::PRECCSBK);
    csbk[2U] = 0x80U; // Data content follows; target is an individual radio.
    csbk[3U] = blocksToFollow;
    csbk[4U] = static_cast<uint8_t>(targetRid >> 16U);
    csbk[5U] = static_cast<uint8_t>(targetRid >> 8U);
    csbk[6U] = static_cast<uint8_t>(targetRid);
    csbk[7U] = static_cast<uint8_t>(sourceRid >> 16U);
    csbk[8U] = static_cast<uint8_t>(sourceRid >> 8U);
    csbk[9U] = static_cast<uint8_t>(sourceRid);

    csbk[10U] ^= CSBK_CRC_MASK[0U];
    csbk[11U] ^= CSBK_CRC_MASK[1U];
    edac::CRC::addCCITT162(csbk, DMR_CSBK_LENGTH_BYTES);
    csbk[10U] ^= CSBK_CRC_MASK[0U];
    csbk[11U] ^= CSBK_CRC_MASK[1U];

    uint8_t payload[DMR_FRAME_LENGTH_BYTES];
    ::memset(payload, 0x00U, sizeof(payload));
    edac::BPTC19696 bptc;
    bptc.encode(csbk, payload);
    addBurstMetadata(payload, DataType::CSBK, colorCode);
    return makeFrame(sourceRid, targetRid, slotNo, sequenceNo, DataType::CSBK, payload);
}

}

std::vector<dmr::data::NetData> BmPacketDataEncoder::encode(uint32_t sourceRid,
    uint32_t targetRid, uint8_t slotNo, uint8_t colorCode, uint8_t sequenceNo,
    const std::vector<uint8_t>& ipPacket, uint8_t preambleCount)
{
    if (sourceRid == 0U || targetRid == 0U || ipPacket.empty()) {
        return {};
    }

    data::DataHeader header;
    header.setDPF(DPF::UNCONFIRMED_DATA);
    header.setA(false);
    header.setSAP(PDUSAP::PACKET_DATA);
    header.setSrcId(sourceRid);
    header.setDstId(targetRid);
    header.setGI(false);
    header.setFullMesage(true);
    header.setSynchronize(false);
    header.setFSN(0U);
    header.setNs(sequenceNo & 0x07U);
    header.calculateLength(kPacketDataRate, static_cast<uint32_t>(ipPacket.size()));

    std::vector<data::NetData> dataFrames;
    auto blockWriter = [&](const void*, const uint8_t currentBlock, const uint8_t* data,
                           uint32_t length, bool) {
        if (data == nullptr) {
            return;
        }

        uint8_t payload[DMR_FRAME_LENGTH_BYTES];
        ::memset(payload, 0x00U, sizeof(payload));
        ::memcpy(payload, data, std::min<uint32_t>(length, sizeof(payload)));
        const DataType::E dataType = currentBlock == 0U ? DataType::DATA_HEADER : kPacketDataRate;
        addBurstMetadata(payload, dataType, colorCode);
        dataFrames.push_back(makeFrame(sourceRid, targetRid, slotNo, 0U, dataType, payload));
    };

    data::Assembler assembler;
    assembler.setBlockWriter(blockWriter);
    assembler.assemble(header, kPacketDataRate, ipPacket.data(), nullptr, nullptr);
    if (dataFrames.empty()) {
        return {};
    }

    const size_t maximumPreambles = 256U - std::min<size_t>(dataFrames.size(), 255U);
    preambleCount = static_cast<uint8_t>(std::min<size_t>(preambleCount, maximumPreambles));

    std::vector<data::NetData> frames;
    frames.reserve(static_cast<size_t>(preambleCount) + dataFrames.size());
    uint8_t frameSequence = 0U;
    for (uint8_t i = 0U; i < preambleCount; ++i) {
        const size_t remaining = dataFrames.size() + static_cast<size_t>(preambleCount) - 1U - i;
        frames.push_back(makePreamble(sourceRid, targetRid, slotNo, colorCode,
            frameSequence++, static_cast<uint8_t>(remaining)));
    }
    for (auto& frame : dataFrames) {
        frame.setSeqNo(frameSequence++);
        frames.push_back(frame);
    }
    return frames;
}
