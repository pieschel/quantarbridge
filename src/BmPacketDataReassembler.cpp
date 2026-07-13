#include "BmPacketDataReassembler.h"

#include "common/dmr/data/DataBlock.h"

#include <algorithm>
#include <array>
#include <cstring>

using namespace dmr::defines;

namespace {
constexpr auto kSessionTimeout = std::chrono::seconds(5);
constexpr auto kPacketDuplicateWindow = std::chrono::seconds(30);
constexpr uint32_t kMaxBlocks = 64U;

bool isPayloadDataType(DataType::E dataType)
{
    return dataType == DataType::RATE_12_DATA ||
        dataType == DataType::RATE_34_DATA ||
        dataType == DataType::RATE_1_DATA;
}
}

uint64_t BmPacketDataReassembler::sessionKey(uint32_t sourceRid, uint32_t targetRid, uint8_t slotNo)
{
    return (static_cast<uint64_t>(sourceRid & 0xFFFFFFU) << 25U) |
        (static_cast<uint64_t>(targetRid & 0xFFFFFFU) << 1U) |
        static_cast<uint64_t>(slotNo == 2U ? 1U : 0U);
}

uint64_t BmPacketDataReassembler::packetSignature(const Packet& packet)
{
    uint64_t hash = 1469598103934665603ULL;
    auto add = [&hash](uint8_t value) {
        hash ^= value;
        hash *= 1099511628211ULL;
    };

    for (uint32_t value : {packet.sourceRid, packet.targetRid}) {
        add(static_cast<uint8_t>(value >> 24U));
        add(static_cast<uint8_t>(value >> 16U));
        add(static_cast<uint8_t>(value >> 8U));
        add(static_cast<uint8_t>(value));
    }
    add(packet.slotNo);
    for (uint8_t value : packet.bytes) {
        add(value);
    }
    return hash;
}

void BmPacketDataReassembler::expire(std::chrono::steady_clock::time_point now)
{
    for (auto it = m_sessions.begin(); it != m_sessions.end();) {
        if (now - it->second.updatedAt > kSessionTimeout) {
            it = m_sessions.erase(it);
        } else {
            ++it;
        }
    }

    for (auto it = m_recentPackets.begin(); it != m_recentPackets.end();) {
        if (now >= it->second) {
            it = m_recentPackets.erase(it);
        } else {
            ++it;
        }
    }
}

BmPacketDataReassembler::Result BmPacketDataReassembler::push(
    const dmr::data::NetData& frame, std::chrono::steady_clock::time_point now)
{
    expire(now);

    uint8_t payload[33U];
    ::memset(payload, 0x00U, sizeof(payload));
    frame.getData(payload);

    const auto dataType = frame.getDataType();
    const uint64_t key = sessionKey(frame.getSrcId(), frame.getDstId(), frame.getSlotNo());

    if (dataType == DataType::DATA_HEADER) {
        dmr::data::DataHeader header;
        if (!header.decode(payload) ||
            (header.getDPF() != DPF::CONFIRMED_DATA && header.getDPF() != DPF::UNCONFIRMED_DATA) ||
            header.getSAP() != PDUSAP::PACKET_DATA ||
            header.getBlocksToFollow() == 0U || header.getBlocksToFollow() > kMaxBlocks) {
            return {};
        }

        Session session;
        session.header = header;
        session.expectedBlocks = header.getBlocksToFollow();
        session.confirmed = header.getDPF() == DPF::CONFIRMED_DATA;
        session.blocks.resize(session.expectedBlocks);
        session.received.resize(session.expectedBlocks, false);
        session.updatedAt = now;
        m_sessions[key] = std::move(session);
        return {true, std::nullopt};
    }

    if (!isPayloadDataType(dataType)) {
        return {};
    }

    auto sessionIt = m_sessions.find(key);
    if (sessionIt == m_sessions.end()) {
        return {};
    }

    Session& session = sessionIt->second;
    session.updatedAt = now;

    dmr::data::DataBlock block;
    block.setDataType(dataType);
    if (!block.decode(payload, session.header)) {
        return {true, std::nullopt};
    }

    std::array<uint8_t, 32U> decoded {};
    const uint32_t decodedLength = block.getData(decoded.data());
    uint32_t blockIndex = session.nextUnconfirmedBlock;
    if (session.confirmed) {
        blockIndex = block.getSerialNo();
    } else {
        ++session.nextUnconfirmedBlock;
    }

    if (blockIndex >= session.expectedBlocks) {
        return {true, std::nullopt};
    }

    if (!session.received[blockIndex]) {
        session.blocks[blockIndex].assign(decoded.begin(), decoded.begin() + decodedLength);
        session.received[blockIndex] = true;
        ++session.receivedBlocks;
    }

    if (session.receivedBlocks != session.expectedBlocks) {
        return {true, std::nullopt};
    }

    Packet packet;
    packet.sourceRid = frame.getSrcId();
    packet.targetRid = frame.getDstId();
    packet.slotNo = static_cast<uint8_t>(frame.getSlotNo());
    packet.sequenceNo = session.header.getNs();
    for (const auto& bytes : session.blocks) {
        packet.bytes.insert(packet.bytes.end(), bytes.begin(), bytes.end());
    }

    const uint32_t padAndCrc = static_cast<uint32_t>(session.header.getPadLength()) + 4U;
    if (packet.bytes.size() >= 20U && (packet.bytes[0U] >> 4U) == 4U) {
        const uint32_t ipv4Length = (static_cast<uint32_t>(packet.bytes[2U]) << 8U) | packet.bytes[3U];
        const uint32_t headerLength = static_cast<uint32_t>(packet.bytes[0U] & 0x0FU) * 4U;
        if (ipv4Length >= headerLength && ipv4Length <= packet.bytes.size()) {
            packet.bytes.resize(ipv4Length);
        }
    } else if (padAndCrc <= packet.bytes.size()) {
        packet.bytes.resize(packet.bytes.size() - padAndCrc);
    }

    m_sessions.erase(sessionIt);
    const uint64_t signature = packetSignature(packet);
    const auto duplicate = m_recentPackets.find(signature);
    if (duplicate != m_recentPackets.end() && now < duplicate->second) {
        return {true, std::nullopt};
    }
    m_recentPackets[signature] = now + kPacketDuplicateWindow;
    return {true, std::move(packet)};
}
