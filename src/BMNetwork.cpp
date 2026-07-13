#include "BMNetwork.h"
#include "BmPacketDataEncoder.h"
#include "BMProtocol.h"

#include "common/edac/SHA256.h"
#include "common/Log.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>

using dmr::data::NetData;
using namespace dmr::defines;
using namespace network::udp;

namespace {
constexpr uint32_t kHomebrewPacketLength = 55U;
constexpr uint32_t kPacketDataFrameIntervalMs = 60U;
constexpr size_t kMaxQueuedPacketDataFrames = 512U;

std::string fixedField(const std::string& value, size_t width)
{
    std::string out = value.substr(0U, width);
    if (out.size() < width) {
        out.append(width - out.size(), ' ');
    }
    return out;
}

double parseCoordinate(const std::string& value)
{
    try {
        return std::stod(value);
    } catch (...) {
        return 0.0;
    }
}

}

BMNetwork::BMNetwork(const BMConfig& config) :
    m_config(config),
    m_socket(config.localPort),
    m_retryTimer(1000U, 10U),
    m_timeoutTimer(1000U, 60U),
    m_beaconTimer(1000U, 5U),
    m_rng(std::random_device{}())
{
    m_repeaterId[0U] = static_cast<uint8_t>(config.repeaterId >> 24);
    m_repeaterId[1U] = static_cast<uint8_t>(config.repeaterId >> 16);
    m_repeaterId[2U] = static_cast<uint8_t>(config.repeaterId >> 8);
    m_repeaterId[3U] = static_cast<uint8_t>(config.repeaterId);
    ::memcpy(m_netId, m_repeaterId, sizeof(m_netId));
    m_streamId[0U] = nextStreamId();
    m_streamId[1U] = nextStreamId();
}

bool BMNetwork::open()
{
    if (Socket::lookup(m_config.address, m_config.port, m_address, m_addressLen) != 0) {
        ::LogError(LOG_NET, "Unable to resolve %s host %s:%u", isGatewayMode() ? "DMRGateway" : "BrandMeister", m_config.address.c_str(), m_config.port);
        m_socketActive = false;
        m_status = isGatewayMode() ? Status::WaitingConfig : Status::WaitingConnect;
        m_retryTimer.start();
        m_timeoutTimer.stop();
        m_beaconTimer.stop();
        return false;
    }

    if (!m_socket.open(m_address)) {
        ::LogError(LOG_NET, "Unable to open %s socket to %s:%u", isGatewayMode() ? "DMRGateway" : "BrandMeister", m_config.address.c_str(), m_config.port);
        m_socketActive = false;
        m_status = isGatewayMode() ? Status::WaitingConfig : Status::WaitingConnect;
        m_retryTimer.start();
        m_timeoutTimer.stop();
        m_beaconTimer.stop();
        return false;
    }

    m_socketActive = true;
    m_status = isGatewayMode() ? Status::WaitingConfig : Status::WaitingConnect;
    if (isGatewayMode()) {
        m_retryTimer.start();
        m_timeoutTimer.stop();
        m_beaconTimer.stop();
        ::LogInfoEx(LOG_NET, "Opened DMRGateway socket to %s:%u", m_config.address.c_str(), m_config.port);
    } else {
        ::LogInfoEx(LOG_NET, "Opened BrandMeister socket to %s:%u", m_config.address.c_str(), m_config.port);

        m_timeoutTimer.stop();
        m_beaconTimer.stop();
        if (writeLogin()) {
            m_status = Status::WaitingLogin;
            m_timeoutTimer.start();
        }
        m_retryTimer.start();
    }
    return true;
}

void BMNetwork::close()
{
    if (!isGatewayMode() && m_status == Status::Running) {
        uint8_t buffer[9U];
        ::memcpy(buffer + 0U, "RPTCL", 5U);
        ::memcpy(buffer + 5U, m_repeaterId, 4U);
        writePacket(buffer, 9U);
    }

    m_socket.close();
    m_socketActive = false;
    m_retryTimer.stop();
    m_timeoutTimer.stop();
    m_beaconTimer.stop();
    m_packetDataTxQueue.clear();
    m_packetDataTxElapsedMs = 0U;
    m_status = isGatewayMode() ? Status::WaitingConfig : Status::WaitingConnect;
}

void BMNetwork::deferReconnect()
{
    close();
    m_status = Status::WaitingConnect;
    m_retryTimer.start();
}

bool BMNetwork::isConnected() const
{
    return isGatewayMode() ? m_status == Status::Running : m_status == Status::Running;
}

bool BMNetwork::read(NetData& data)
{
    if (!isConnected() || m_rxPackets.empty()) {
        return false;
    }

    const std::vector<uint8_t> packet = std::move(m_rxPackets.front());
    m_rxPackets.pop_front();
    const uint32_t length = static_cast<uint32_t>(packet.size());
    if (length < kHomebrewPacketLength) {
        return false;
    }

    const uint8_t seqNo = packet[4U];
    const uint32_t srcId = (static_cast<uint32_t>(packet[5U]) << 16) | (static_cast<uint32_t>(packet[6U]) << 8) | packet[7U];
    const uint32_t dstId = (static_cast<uint32_t>(packet[8U]) << 16) | (static_cast<uint32_t>(packet[9U]) << 8) | packet[10U];
    const uint32_t slotNo = (packet[15U] & 0x80U) ? 2U : 1U;
    const auto flco = (packet[15U] & 0x40U) ? FLCO::PRIVATE : FLCO::GROUP;

    data.setSeqNo(seqNo);
    data.setSrcId(srcId);
    data.setDstId(dstId);
    data.setSlotNo(slotNo);
    data.setFLCO(flco);
    data.setBER(packet[53U]);
    data.setRSSI(packet[54U]);
    data.setControl(0U);

    const bool dataSync = (packet[15U] & 0x20U) == 0x20U;
    const bool voiceSync = (packet[15U] & 0x10U) == 0x10U;
    if (dataSync) {
        data.setDataType(static_cast<DataType::E>(packet[15U] & 0x0FU));
        data.setN(0U);
    } else if (voiceSync) {
        data.setDataType(DataType::VOICE_SYNC);
        data.setN(0U);
    } else {
        data.setDataType(DataType::VOICE);
        data.setN(packet[15U] & 0x0FU);
    }

    data.setData(packet.data() + 20U);
    return true;
}

bool BMNetwork::write(const NetData& data)
{
    if (!isConnected()) {
        return false;
    }

    const uint32_t slotNo = data.getSlotNo();
    if ((slotNo != 1U && slotNo != 2U) ||
        (slotNo == 1U && !m_config.slot1) || (slotNo == 2U && !m_config.slot2)) {
        return false;
    }

    const uint32_t slotIndex = slotNo - 1U;
    const auto dataType = data.getDataType();
    if (dataType == DataType::VOICE_LC_HEADER || dataType == DataType::CSBK || dataType == DataType::DATA_HEADER) {
        m_streamId[slotIndex] = nextStreamId();
    }

    const std::vector<uint8_t> packet = buildHomebrewPacket(data, m_streamId[slotIndex]);
    return !packet.empty() && writePacket(packet.data(), static_cast<uint32_t>(packet.size()));
}

std::vector<uint8_t> BMNetwork::buildHomebrewPacket(const NetData& data, uint32_t streamId) const
{
    const uint32_t slotNo = data.getSlotNo();
    if (slotNo != 1U && slotNo != 2U) {
        return {};
    }

    std::vector<uint8_t> buffer(kHomebrewPacketLength, 0x00U);
    ::memcpy(buffer.data() + 0U, "DMRD", 4U);

    const uint32_t srcId = data.getSrcId();
    const uint32_t dstId = data.getDstId();
    const auto dataType = data.getDataType();
    buffer[4U] = data.getSeqNo();
    buffer[5U] = static_cast<uint8_t>(srcId >> 16);
    buffer[6U] = static_cast<uint8_t>(srcId >> 8);
    buffer[7U] = static_cast<uint8_t>(srcId);
    buffer[8U] = static_cast<uint8_t>(dstId >> 16);
    buffer[9U] = static_cast<uint8_t>(dstId >> 8);
    buffer[10U] = static_cast<uint8_t>(dstId);
    ::memcpy(buffer.data() + 11U, m_repeaterId, 4U);

    buffer[15U] = (slotNo == 2U) ? 0x80U : 0x00U;
    buffer[15U] |= (data.getFLCO() == FLCO::GROUP) ? 0x00U : 0x40U;
    if (dataType == DataType::VOICE_SYNC) {
        buffer[15U] |= 0x10U;
    } else if (dataType == DataType::VOICE) {
        buffer[15U] |= (data.getN() & 0x0FU);
    } else {
        buffer[15U] |= (0x20U | static_cast<uint8_t>(dataType));
    }

    ::memcpy(buffer.data() + 16U, &streamId, 4U);
    data.getData(buffer.data() + 20U);
    buffer[53U] = data.getBER();
    buffer[54U] = data.getRSSI();
    return buffer;
}

bool BMNetwork::writePacketData(uint32_t sourceRid, uint32_t targetRid, uint8_t slotNo, const std::vector<uint8_t>& ipPacket)
{
    if (ipPacket.empty()) {
        ::LogWarning(LOG_NET, "BM packet-data send skipped because IP packet is empty");
        return false;
    }

    if (!isConnected()) {
        ::LogWarning(LOG_NET, "BM packet-data send skipped because BrandMeister is not connected");
        return false;
    }

    const uint8_t effectiveSlot = m_config.timeslot;
    if ((slotNo == 1U || slotNo == 2U) && slotNo != effectiveSlot) {
        ::LogDebug(LOG_NET, "Remapping BM packet-data from slot %u to configured slot %u", slotNo, effectiveSlot);
    }

    uint8_t& ns = m_packetDataNs[sourceRid];
    ns = static_cast<uint8_t>((ns + 1U) & 0x07U);
    const std::vector<NetData> frames = BmPacketDataEncoder::encode(sourceRid, targetRid,
        effectiveSlot, static_cast<uint8_t>(m_config.colorCode), ns, ipPacket);

    if (frames.empty()) {
        ::LogWarning(LOG_NET, "BM packet-data send produced no DMR frames srcId=%u dstId=%u", sourceRid, targetRid);
        return false;
    }

    if (m_packetDataTxQueue.size() + frames.size() > kMaxQueuedPacketDataFrames) {
        ::LogWarning(LOG_NET, "BM packet-data send queue is full srcId=%u dstId=%u queued=%u new=%u",
            sourceRid, targetRid, static_cast<uint32_t>(m_packetDataTxQueue.size()),
            static_cast<uint32_t>(frames.size()));
        return false;
    }

    const bool startImmediately = m_packetDataTxQueue.empty();
    const uint32_t streamId = nextStreamId();
    for (const auto& frame : frames) {
        auto packet = buildHomebrewPacket(frame, streamId);
        if (packet.empty()) {
            return false;
        }
        m_packetDataTxQueue.push_back(std::move(packet));
    }
    if (startImmediately) {
        m_packetDataTxElapsedMs = kPacketDataFrameIntervalMs;
    }

    ::LogInfoEx(LOG_NET,
        "Queued BM packet-data srcId=%u dstId=%u slot=%u ipLen=%u frames=%u intervalMs=%u streamId=%u",
        sourceRid, targetRid, effectiveSlot, static_cast<uint32_t>(ipPacket.size()),
        static_cast<uint32_t>(frames.size()), kPacketDataFrameIntervalMs, streamId);

    return true;
}

void BMNetwork::clockPacketData(uint32_t ms)
{
    if (!isConnected() || m_packetDataTxQueue.empty()) {
        return;
    }

    m_packetDataTxElapsedMs = std::min<uint32_t>(
        m_packetDataTxElapsedMs + ms, kPacketDataFrameIntervalMs * 2U);
    if (m_packetDataTxElapsedMs < kPacketDataFrameIntervalMs) {
        return;
    }

    const std::vector<uint8_t>& packet = m_packetDataTxQueue.front();
    if (!writePacket(packet.data(), static_cast<uint32_t>(packet.size()))) {
        ::LogWarning(LOG_NET, "BM packet-data queued frame send failed; retaining %u frames",
            static_cast<uint32_t>(m_packetDataTxQueue.size()));
        m_packetDataTxElapsedMs = kPacketDataFrameIntervalMs;
        return;
    }

    m_packetDataTxQueue.pop_front();
    m_packetDataTxElapsedMs -= kPacketDataFrameIntervalMs;
    if (m_packetDataTxQueue.empty()) {
        m_packetDataTxElapsedMs = 0U;
        ::LogInfoEx(LOG_NET, "BM packet-data transmission complete");
    }
}

void BMNetwork::clock(uint32_t ms)
{
    auto stateName = [this]() -> const char* {
        switch (m_status) {
        case Status::WaitingConnect:
            return "WaitingConnect";
        case Status::WaitingLogin:
            return "WaitingLogin";
        case Status::WaitingAuthorisation:
            return "WaitingAuthorisation";
        case Status::WaitingConfig:
            return "WaitingConfig";
        case Status::WaitingOptions:
            return "WaitingOptions";
        case Status::Running:
            return "Running";
        default:
            return "Unknown";
        }
    };

    if (!isGatewayMode() && m_status == Status::WaitingConnect) {
        m_retryTimer.clock(ms);
        if (m_retryTimer.isRunning() && m_retryTimer.hasExpired()) {
            if (!m_socketActive) {
                open();
                return;
            }

            if (!writeLogin()) {
                return;
            }

            m_status = Status::WaitingLogin;
            m_timeoutTimer.start();
            m_retryTimer.start();
        }

        return;
    }

    {
        sockaddr_storage rxAddress {};
        uint32_t rxAddressLen = 0U;
        const int length = m_socket.read(m_buffer, sizeof(m_buffer), rxAddress, rxAddressLen);
        if (length < 0) {
            ::LogError(LOG_NET, "%s socket read failed, reconnecting", isGatewayMode() ? "DMRGateway" : "BrandMeister");
            close();
            if (isGatewayMode()) {
                open();
            } else {
                m_retryTimer.start();
            }
            return;
        }

        if (length != 0) {

            if (isGatewayMode()) {
                if (!Socket::match(m_address, rxAddress)) {
                    ::LogWarning(LOG_NET, "Ignoring packet from unexpected DMRGateway source");
                } else if (::memcmp(m_buffer, "DMRD", 4U) == 0) {
                    parseIncomingDMR(m_buffer, static_cast<uint32_t>(length));
                } else if (::memcmp(m_buffer, "DMRP", 4U) == 0) {
                    if (m_status != Status::Running) {
                        ::LogInfoEx(LOG_NET, "DMRGateway local link established");
                    }
                    m_status = Status::Running;
                    m_retryTimer.stop();
                } else if (::memcmp(m_buffer, "DMRB", 4U) == 0) {
                    ::LogInfoEx(LOG_NET, "DMRGateway requested a local beacon");
                } else if (::memcmp(m_buffer, "DMRG", 4U) == 0 || ::memcmp(m_buffer, "DMRA", 4U) == 0) {
                    // GPS / talker alias packets are not consumed by the current bridge.
                } else {
                    ::LogWarning(LOG_NET, "Received unknown DMRGateway packet type");
                }
            } else {
                char tag[8U];
                ::memset(tag, 0x00U, sizeof(tag));
                const uint32_t tagLength = length >= 7 ? 7U : static_cast<uint32_t>(length);
                ::memcpy(tag, m_buffer, tagLength);
                const bool routinePacket = ::memcmp(m_buffer, "DMRD", 4U) == 0 ||
                    ::memcmp(m_buffer, "MSTPONG", 7U) == 0 || ::memcmp(m_buffer, "RPTSBKN", 7U) == 0;
                if (routinePacket) {
                    ::LogDebug(LOG_NET, "BrandMeister RX packet '%s' len=%u while state=%s", tag, static_cast<uint32_t>(length), stateName());
                } else {
                    ::LogInfoEx(LOG_NET, "BrandMeister RX packet '%s' len=%u while state=%s", tag, static_cast<uint32_t>(length), stateName());
                }

                if (!Socket::match(m_address, rxAddress)) {
                    ::LogWarning(LOG_NET, "Ignoring packet from unexpected BrandMeister source");
                } else if (::memcmp(m_buffer, "DMRD", 4U) == 0) {
                    if (m_status != Status::Running) {
                        m_status = Status::Running;
                        m_timeoutTimer.start();
                        m_retryTimer.start();
                        ::LogInfoEx(LOG_NET, "BrandMeister traffic detected, promoting session to running state");
                    }
                    parseIncomingDMR(m_buffer, static_cast<uint32_t>(length));
                } else if (::memcmp(m_buffer, "MSTNAK", 6U) == 0) {
                    ::LogWarning(LOG_NET, "BrandMeister login rejected, restarting handshake");
                    m_status = Status::WaitingLogin;
                    m_timeoutTimer.start();
                    m_retryTimer.start();
                } else if (::memcmp(m_buffer, "RPTACK", 6U) == 0) {
                    switch (m_status) {
                    case Status::WaitingLogin:
                        ::memcpy(m_salt, m_buffer + 6U, sizeof(m_salt));
                        writeAuthorisation();
                        m_status = Status::WaitingAuthorisation;
                        m_timeoutTimer.start();
                        m_retryTimer.start();
                        break;
                    case Status::WaitingAuthorisation:
                        writeConfig();
                        m_status = Status::WaitingConfig;
                        ::LogInfoEx(LOG_NET, "BrandMeister configuration sent, waiting for acknowledgement");
                        m_timeoutTimer.start();
                        m_retryTimer.start();
                        break;
                    case Status::WaitingConfig:
                        if (!m_config.options.empty()) {
                            writeOptions();
                            m_status = Status::WaitingOptions;
                            m_timeoutTimer.start();
                            m_retryTimer.start();
                        } else {
                            m_status = Status::Running;
                            ::LogInfoEx(LOG_NET, "BrandMeister login complete");
                            m_timeoutTimer.start();
                            m_retryTimer.start();
                        }
                        break;
                    case Status::WaitingOptions:
                        m_status = Status::Running;
                        ::LogInfoEx(LOG_NET, "BrandMeister login complete");
                        m_timeoutTimer.start();
                        m_retryTimer.start();
                        break;
                    default:
                        break;
                    }
                } else if (::memcmp(m_buffer, "MSTPONG", 7U) == 0) {
                    m_timeoutTimer.start();
                } else if (::memcmp(m_buffer, "RPTSBKN", 7U) == 0) {
                    // BrandMeister asks MMDVM repeaters to transmit a local DMR beacon.
                    // This cross-mode bridge has no local DMR RF modem, so no action is required.
                } else if (::memcmp(m_buffer, "MSTCL", 5U) == 0) {
                    ::LogWarning(LOG_NET, "BrandMeister closed the session, retrying in 10 seconds");
                    deferReconnect();
                    return;
                } else {
                    ::LogWarning(LOG_NET, "Received unknown BrandMeister packet type");
                }
            }
        }
    }

    clockPacketData(ms);

    if (isGatewayMode()) {
        if (m_status == Status::Running) {
            m_retryTimer.stop();
            return;
        }

        m_retryTimer.clock(ms);
        if (m_retryTimer.isRunning() && m_retryTimer.hasExpired()) {
            writeConfig();
            m_retryTimer.start();
        }
    } else {
        m_retryTimer.clock(ms);
        if (m_retryTimer.isRunning() && m_retryTimer.hasExpired()) {
            switch (m_status) {
            case Status::WaitingLogin:
                writeLogin();
                break;
            case Status::WaitingAuthorisation:
                writeAuthorisation();
                break;
            case Status::WaitingConfig:
                writeConfig();
                break;
            case Status::WaitingOptions:
                writeOptions();
                break;
            case Status::Running:
                writePing();
                break;
            case Status::WaitingConnect:
                break;
            }
            m_retryTimer.start();
        }

        m_timeoutTimer.clock(ms);
        if (m_timeoutTimer.isRunning() && m_timeoutTimer.hasExpired()) {
            ::LogError(LOG_NET, "BrandMeister connection timed out, retrying in 10 seconds");
            deferReconnect();
        }
    }
}

bool BMNetwork::writeLogin()
{
    uint8_t buffer[8U];
    ::memcpy(buffer + 0U, "RPTL", 4U);
    ::memcpy(buffer + 4U, m_repeaterId, 4U);
    ::LogInfoEx(LOG_NET, "Sending BrandMeister login for repeater %u", m_config.repeaterId);
    return writePacket(buffer, sizeof(buffer));
}

bool BMNetwork::writeAuthorisation()
{
    uint8_t input[128U];
    ::memcpy(input, m_salt, sizeof(m_salt));
    ::memcpy(input + sizeof(m_salt), m_config.password.data(), m_config.password.size());

    uint8_t buffer[40U];
    ::memcpy(buffer + 0U, "RPTK", 4U);
    ::memcpy(buffer + 4U, m_repeaterId, 4U);
    edac::SHA256 sha256;
    sha256.buffer(input, static_cast<uint32_t>(sizeof(m_salt) + m_config.password.size()), buffer + 8U);
    return writePacket(buffer, sizeof(buffer));
}

bool BMNetwork::writeConfig()
{
    if (isGatewayMode()) {
        char buffer[150U];
        ::memset(buffer, 0x00U, sizeof(buffer));
        ::memcpy(buffer + 0U, "DMRC", 4U);
        ::memcpy(buffer + 4U, m_repeaterId, 4U);
        ::snprintf(buffer + 8U, sizeof(buffer) - 8U, "%-8.8s%09u%09u%02u%02u%c%-40.40s%-40.40s",
            m_config.callsign.c_str(),
            m_config.rxFrequency,
            m_config.txFrequency,
            m_config.power > 99U ? 99U : m_config.power,
            m_config.colorCode > 99U ? 99U : m_config.colorCode,
            '4',
            m_config.softwareId.c_str(),
            m_config.packageId.c_str());

        ::LogInfoEx(LOG_NET, "Sending DMRGateway local config for repeater %u", m_config.repeaterId);
        return writePacket(reinterpret_cast<const uint8_t*>(buffer), 119U);
    }

    const double latitude = parseCoordinate(m_config.latitude);
    const double longitude = parseCoordinate(m_config.longitude);
    const uint32_t height = static_cast<uint32_t>(std::max(0, std::min(999, std::atoi(m_config.height.c_str()))));

    char payload[295U];
    ::memset(payload, 0x00U, sizeof(payload));
    ::snprintf(payload, sizeof(payload),
        "%-8.8s%09u%09u%02u%02u%+08.4f%+09.4f%03u%-20.20s%-19.19s%c%-124.124s%40.40s%40.40s",
        m_config.callsign.c_str(),
        m_config.rxFrequency,
        m_config.txFrequency,
        m_config.power > 99U ? 99U : m_config.power,
        m_config.colorCode > 99U ? 99U : m_config.colorCode,
        latitude,
        longitude,
        height,
        m_config.location.c_str(),
        m_config.description.c_str(),
            bm::protocol::slotMode(m_config),
        m_config.url.c_str(),
        m_config.softwareId.c_str(),
        m_config.packageId.c_str());

    const uint32_t payloadLength = static_cast<uint32_t>(::strlen(payload));

    uint8_t buffer[310U];
    ::memcpy(buffer + 0U, "RPTC", 4U);
    ::memcpy(buffer + 4U, m_repeaterId, 4U);
    ::memcpy(buffer + 8U, payload, payloadLength);
    ::LogInfoEx(LOG_NET, "Sending BrandMeister configuration packet len=%u slots=%c payload='%s'", 8U + payloadLength, bm::protocol::slotMode(m_config), payload);
    return writePacket(buffer, static_cast<uint32_t>(8U + payloadLength));
}

bool BMNetwork::writeOptions()
{
    std::string options = m_config.options;
    uint8_t buffer[512U];
    ::memcpy(buffer + 0U, "RPTO", 4U);
    ::memcpy(buffer + 4U, m_repeaterId, 4U);
    ::memcpy(buffer + 8U, options.data(), options.size());
    return writePacket(buffer, static_cast<uint32_t>(8U + options.size()));
}

bool BMNetwork::writePing()
{
    uint8_t buffer[11U];
    ::memcpy(buffer + 0U, "RPTPING", 7U);
    ::memcpy(buffer + 7U, m_repeaterId, 4U);
    return writePacket(buffer, sizeof(buffer));
}

bool BMNetwork::writeGatewayPong()
{
    return writePacket(reinterpret_cast<const uint8_t*>("DMRP"), 4U);
}

bool BMNetwork::writeGatewayBeacon()
{
    return writePacket(reinterpret_cast<const uint8_t*>("DMRB"), 4U);
}

bool BMNetwork::writePacket(const uint8_t* data, uint32_t length)
{
    return m_socket.write(data, length, m_address, m_addressLen);
}

void BMNetwork::parseIncomingDMR(const uint8_t* buffer, uint32_t length)
{
    if (length < kHomebrewPacketLength) {
        return;
    }

    const uint8_t slotNo = (buffer[15U] & 0x80U) != 0U ? 2U : 1U;
    if (slotNo != m_config.timeslot) {
        ::LogDebug(LOG_NET, "Ignoring BrandMeister DMR packet on disabled slot %u", slotNo);
        return;
    }

    if (m_rxPackets.size() >= 2048U) {
        ::LogError(LOG_NET, "BrandMeister RX packet queue overflow, dropping oldest packet");
        m_rxPackets.pop_front();
    }

    m_rxPackets.emplace_back(buffer, buffer + length);
}

bool BMNetwork::isGatewayMode() const
{
    return m_config.transport == "mmdvm-gateway";
}

uint32_t BMNetwork::nextStreamId()
{
    std::uniform_int_distribution<uint32_t> dist(1U, 0xFFFFFFFEU);
    return dist(m_rng);
}
