#include "AppConfig.h"
#include "BMNetwork.h"
#include "BMProtocol.h"
#include "BmPacketDataReassembler.h"
#include "SmsSubsystem.h"

#include "common/Log.h"
#include "common/Utils.h"
#include "common/network/Network.h"
#include "common/dmr/data/DataHeader.h"
#include "common/dmr/data/EMB.h"
#include "common/dmr/data/EmbeddedData.h"
#include "common/dmr/data/NetData.h"
#include "common/dmr/lc/FullLC.h"

#include <algorithm>
#include <atomic>
#include <array>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <mutex>
#include <optional>
#include <sstream>
#include <thread>
#include <unordered_map>
#include <limits>

using namespace dmr::defines;

namespace {
std::atomic<bool> g_running {true};
// BM downlink can arrive in adjacent segments with a short idle/terminator gap in between.
// Keep this tight so a delayed downlink terminator does not hold the local RF uplink busy.
constexpr auto kCallMergeWindow = std::chrono::milliseconds(250);
constexpr auto kBmEchoSuppressWindow = std::chrono::seconds(20);
constexpr auto kFneDuplicateSuppressWindow = std::chrono::milliseconds(15);
constexpr auto kBmDuplicateSuppressWindow = std::chrono::milliseconds(15);
constexpr auto kBmHeaderSuppressWindow = std::chrono::milliseconds(750);
constexpr auto kBmDynamicReleaseGrace = std::chrono::seconds(3);
constexpr auto kDynamicStateFlushInterval = std::chrono::seconds(5);
constexpr auto kBmApiRepairCooldown = std::chrono::seconds(10);
constexpr auto kBmStartupCleanupDelay = std::chrono::seconds(2);
constexpr uint32_t kMaxFnePacketsPerLoop = 64U;
constexpr uint32_t kMaxBmPacketsPerLoop = 64U;
constexpr size_t kMaxBmApiTasks = 32U;

class AsyncTaskQueue {
public:
    AsyncTaskQueue() :
        m_worker([this]() { run(); })
    {
    }

    ~AsyncTaskQueue()
    {
        shutdown();
    }

    bool enqueue(std::function<void()> task)
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (m_stopping || m_tasks.size() >= kMaxBmApiTasks) {
            return false;
        }

        m_tasks.push_back(std::move(task));
        m_ready.notify_one();
        return true;
    }

    void shutdown()
    {
        {
            std::lock_guard<std::mutex> lock(m_mutex);
            if (m_stopping) {
                return;
            }
            m_stopping = true;
            m_tasks.clear();
        }
        m_ready.notify_one();
        if (m_worker.joinable()) {
            m_worker.join();
        }
    }

private:
    void run()
    {
        while (true) {
            std::function<void()> task;
            {
                std::unique_lock<std::mutex> lock(m_mutex);
                m_ready.wait(lock, [this]() { return m_stopping || !m_tasks.empty(); });
                if (m_stopping) {
                    return;
                }
                task = std::move(m_tasks.front());
                m_tasks.pop_front();
            }

            try {
                task();
            } catch (const std::exception& ex) {
                ::LogError(LOG_HOST, "Asynchronous task failed: %s", ex.what());
            } catch (...) {
                ::LogError(LOG_HOST, "Asynchronous task failed with an unknown error");
            }
        }
    }

    std::mutex m_mutex;
    std::condition_variable m_ready;
    std::deque<std::function<void()>> m_tasks;
    bool m_stopping {false};
    std::thread m_worker;
};

void handleSignal(int)
{
    g_running = false;
}

dmr::data::NetData parseFNERawDMR(const uint8_t* buffer, uint32_t length)
{
    if (length < 55U || ::memcmp(buffer, "DMRD", 4U) != 0) {
        throw std::runtime_error("Invalid FNE DMR payload");
    }

    dmr::data::NetData data;
    data.setSeqNo(buffer[4U]);
    data.setSrcId((static_cast<uint32_t>(buffer[5U]) << 16) | (static_cast<uint32_t>(buffer[6U]) << 8) | buffer[7U]);
    data.setDstId((static_cast<uint32_t>(buffer[8U]) << 16) | (static_cast<uint32_t>(buffer[9U]) << 8) | buffer[10U]);
    data.setControl(buffer[14U]);
    data.setSlotNo((buffer[15U] & 0x80U) ? 2U : 1U);
    data.setFLCO((buffer[15U] & 0x40U) ? FLCO::PRIVATE : FLCO::GROUP);
    data.setBER(buffer[53U]);
    data.setRSSI(buffer[54U]);

    const bool dataSync = (buffer[15U] & 0x20U) == 0x20U;
    const bool voiceSync = (buffer[15U] & 0x10U) == 0x10U;
    if (dataSync) {
        data.setDataType(static_cast<DataType::E>(buffer[15U] & 0x0FU));
        data.setN(0U);
    } else if (voiceSync) {
        data.setDataType(DataType::VOICE_SYNC);
        data.setN(0U);
    } else {
        data.setDataType(DataType::VOICE);
        data.setN(buffer[15U] & 0x0FU);
    }

    data.setData(buffer + 20U);
    return data;
}

bool decodeHexKey(const std::string& hex, uint8_t* output, size_t length)
{
    if (hex.size() != length * 2U) {
        return false;
    }

    auto nibble = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
        if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
        return -1;
    };

    for (size_t i = 0; i < length; ++i) {
        const int hi = nibble(hex[i * 2U]);
        const int lo = nibble(hex[i * 2U + 1U]);
        if (hi < 0 || lo < 0) {
            return false;
        }
        output[i] = static_cast<uint8_t>((hi << 4) | lo);
    }

    return true;
}

std::string bytesToHex(const uint8_t* data, size_t length)
{
    std::ostringstream out;
    out << std::hex << std::setfill('0');
    for (size_t i = 0U; i < length; ++i) {
        out << std::setw(2) << static_cast<uint32_t>(data[i]);
    }
    return out.str();
}

struct PendingTerminator {
    dmr::data::NetData data;
    uint32_t srcId {0U};
    uint32_t dstId {0U};
    dmr::defines::FLCO::E flco {FLCO::GROUP};
    std::chrono::steady_clock::time_point expiresAt {};
};

struct RecentHeader {
    uint32_t srcId {0U};
    uint32_t dstId {0U};
    uint32_t slotNo {0U};
    dmr::defines::FLCO::E flco {FLCO::GROUP};
    std::chrono::steady_clock::time_point expiresAt {};
};

struct DynamicTalkgroupState {
    std::chrono::steady_clock::time_point lastSeenSteady {};
    std::chrono::steady_clock::time_point lastBmSeenSteady {};
    std::chrono::system_clock::time_point lastSeenWall {};
    uint32_t lastSrcId {0U};
    bool expiredPendingBmRelease {false};
    bool bmReleaseRequested {false};
};

size_t hashCombine(size_t seed, size_t value)
{
    return seed ^ (value + 0x9e3779b97f4a7c15ULL + (seed << 6U) + (seed >> 2U));
}

size_t dmrFrameSignature(const dmr::data::NetData& data)
{
    uint8_t payload[33U];
    ::memset(payload, 0x00U, sizeof(payload));
    data.getData(payload);

    size_t signature = 0U;
    signature = hashCombine(signature, data.getSeqNo());
    signature = hashCombine(signature, data.getSrcId());
    signature = hashCombine(signature, data.getDstId());
    signature = hashCombine(signature, data.getSlotNo());
    signature = hashCombine(signature, static_cast<uint32_t>(data.getFLCO()));
    signature = hashCombine(signature, static_cast<uint32_t>(data.getDataType()));
    signature = hashCombine(signature, data.getN());

    for (uint8_t byte : payload) {
        signature = hashCombine(signature, byte);
    }

    return signature;
}

bool isDmrPacketDataFrame(const dmr::data::NetData& data)
{
    const auto dataType = data.getDataType();
    return dataType == DataType::CSBK ||
        dataType == DataType::DATA_HEADER ||
        dataType == DataType::RATE_12_DATA ||
        dataType == DataType::RATE_34_DATA ||
        dataType == DataType::RATE_1_DATA;
}

bool rewriteEmbeddedVoiceLinkControl(dmr::data::NetData& data, uint32_t oldDstId, uint32_t newDstId, const char* direction)
{
    const auto dataType = data.getDataType();
    if (dataType != DataType::VOICE_LC_HEADER && dataType != DataType::TERMINATOR_WITH_LC) {
        return false;
    }

    uint8_t payload[33U];
    ::memset(payload, 0x00U, sizeof(payload));
    data.getData(payload);

    dmr::lc::FullLC fullLC;
    auto lc = fullLC.decode(payload, dataType);
    if (!lc) {
        ::LogWarning(LOG_HOST, "Unable to decode embedded DMR LC while mapping %s oldDstId=%u newDstId=%u type=%u",
            direction, oldDstId, newDstId, static_cast<uint32_t>(dataType));
        return false;
    }

    if (lc->getFLCO() != FLCO::GROUP || lc->getDstId() != oldDstId) {
        return false;
    }

    lc->setDstId(newDstId);
    fullLC.encode(*lc, payload, dataType);
    data.setData(payload);
    ::LogInfoEx(LOG_HOST, "Rewrote embedded DMR LC for %s oldDstId=%u newDstId=%u type=%u",
        direction, oldDstId, newDstId, static_cast<uint32_t>(dataType));
    return true;
}

bool rewriteGeneratedEmbeddedVoiceLinkControl(dmr::data::NetData& data,
    dmr::data::EmbeddedData& embeddedData,
    uint32_t oldDstId,
    uint32_t newDstId,
    const char* direction)
{
    if (data.getFLCO() != FLCO::GROUP) {
        return false;
    }

    if (data.getDataType() == DataType::VOICE_LC_HEADER) {
        dmr::lc::LC lc(FLCO::GROUP, data.getSrcId(), newDstId);
        embeddedData.setLC(lc);
        return false;
    }

    if (data.getDataType() != DataType::VOICE) {
        return false;
    }

    const uint8_t n = data.getN();
    if (n < 1U || n > 4U) {
        return false;
    }

    uint8_t payload[33U];
    ::memset(payload, 0x00U, sizeof(payload));
    data.getData(payload);

    const uint8_t lcss = embeddedData.getData(payload, n);
    dmr::data::EMB emb;
    emb.decode(payload);
    emb.setLCSS(lcss);
    emb.encode(payload);

    data.setData(payload);
    ::LogInfoEx(LOG_HOST, "Rewrote generated embedded DMR LC for %s oldDstId=%u newDstId=%u n=%u lcss=%u",
        direction, oldDstId, newDstId, n, lcss);
    return true;
}

using DynamicWallClockMap = std::unordered_map<uint32_t, std::chrono::system_clock::time_point>;

std::string getCurrentBootId()
{
    std::ifstream input("/proc/sys/kernel/random/boot_id");
    if (!input.is_open()) {
        return {};
    }

    std::string bootId;
    std::getline(input, bootId);
    return bootId;
}

std::filesystem::path getDynamicStatePath(const std::string& configPath)
{
    const auto configFile = std::filesystem::path(configPath);
    if (configFile.has_parent_path()) {
        return configFile.parent_path() / "dynamic_routes.state";
    }

    return std::filesystem::path("dynamic_routes.state");
}

std::filesystem::path getDynamicExpiryTriggerPath(const std::string& configPath)
{
    const auto configFile = std::filesystem::path(configPath);
    if (configFile.has_parent_path()) {
        return configFile.parent_path() / "dynamic_expired.trigger";
    }

    return std::filesystem::path("dynamic_expired.trigger");
}

std::filesystem::path getBmApiKeyPath(const std::string& configPath)
{
    const auto configFile = std::filesystem::path(configPath);
    if (configFile.has_parent_path()) {
        return configFile.parent_path() / "bm_api.key";
    }

    return std::filesystem::path("bm_api.key");
}

std::filesystem::path getDynamicActiveTriggerPath(const std::string& configPath)
{
    const auto configFile = std::filesystem::path(configPath);
    if (configFile.has_parent_path()) {
        return configFile.parent_path() / "dynamic_active.trigger";
    }

    return std::filesystem::path("dynamic_active.trigger");
}

void loadDynamicTalkgroupState(const std::filesystem::path& statePath,
    std::unordered_map<uint32_t, std::chrono::steady_clock::time_point>& dynamicTalkgroups,
    DynamicWallClockMap& dynamicTalkgroupsWall,
    const std::chrono::seconds& dynamicTimeout,
    const std::string& currentBootId)
{
    std::ifstream input(statePath);
    if (!input.is_open()) {
        return;
    }

    const auto nowWall = std::chrono::system_clock::now();
    const auto nowSteady = std::chrono::steady_clock::now();

    std::string line;
    bool firstLine = true;
    while (std::getline(input, line)) {
        if (line.empty()) {
            continue;
        }

        if (firstLine && line.rfind("# boot_id=", 0U) == 0U) {
            firstLine = false;
            const std::string savedBootId = line.substr(10U);
            if (!currentBootId.empty() && savedBootId != currentBootId) {
                return;
            }
            continue;
        }

        firstLine = false;

        std::istringstream stream(line);
        uint32_t talkgroup = 0U;
        long long epochSeconds = 0LL;
        if (!(stream >> talkgroup >> epochSeconds) || talkgroup == 0U) {
            continue;
        }

        const auto lastActiveWall = std::chrono::system_clock::time_point(std::chrono::seconds(epochSeconds));
        const auto age = nowWall - lastActiveWall;
        if (age < std::chrono::system_clock::duration::zero() || age > dynamicTimeout) {
            continue;
        }

        dynamicTalkgroups[talkgroup] = nowSteady - std::chrono::duration_cast<std::chrono::steady_clock::duration>(age);
        dynamicTalkgroupsWall[talkgroup] = lastActiveWall;
    }
}

void saveDynamicTalkgroupState(const std::filesystem::path& statePath, const DynamicWallClockMap& dynamicTalkgroupsWall, const std::string& currentBootId)
{
    const auto tempPath = statePath.string() + ".tmp";
    {
        std::ofstream output(tempPath, std::ios::trunc);
        if (!output.is_open()) {
            return;
        }

        if (!currentBootId.empty()) {
            output << "# boot_id=" << currentBootId << '\n';
        }

        for (const auto& entry : dynamicTalkgroupsWall) {
            const auto epochSeconds = std::chrono::duration_cast<std::chrono::seconds>(entry.second.time_since_epoch()).count();
            output << entry.first << ' ' << epochSeconds << '\n';
        }
    }

    std::error_code ec;
    std::filesystem::rename(tempPath, statePath, ec);
    if (ec) {
        std::filesystem::remove(statePath, ec);
        ec.clear();
        std::filesystem::rename(tempPath, statePath, ec);
        if (ec) {
            std::filesystem::remove(tempPath, ec);
        }
    }
}

uint32_t selectActiveDynamicTalkgroup(const std::unordered_map<uint32_t, DynamicTalkgroupState>& dynamicTalkgroups,
    const std::chrono::steady_clock::time_point& now,
    const std::chrono::steady_clock::duration& dynamicTimeout)
{
    uint32_t selectedTalkgroup = 0U;
    auto newestSeen = std::chrono::steady_clock::time_point::min();

    for (const auto& entry : dynamicTalkgroups) {
        if (entry.second.expiredPendingBmRelease || (now - entry.second.lastSeenSteady) > dynamicTimeout) {
            continue;
        }

        if (entry.second.lastSeenSteady >= newestSeen) {
            newestSeen = entry.second.lastSeenSteady;
            selectedTalkgroup = entry.first;
        }
    }

    return selectedTalkgroup;
}

void writeDynamicExpiryTrigger(const std::filesystem::path& triggerPath, uint32_t talkgroup)
{
    std::ofstream output(triggerPath, std::ios::trunc);
    if (!output.is_open()) {
        return;
    }

    output << talkgroup << '\n';
}

void writeDynamicActiveTrigger(const std::filesystem::path& triggerPath, uint32_t talkgroup)
{
    std::ofstream output(triggerPath, std::ios::trunc);
    if (!output.is_open()) {
        return;
    }

    output << talkgroup << '\n';
}

bool publishGatewayDynTG(uint32_t talkgroup)
{
    std::ostringstream command;
    command << "/usr/bin/mosquitto_pub -h 127.0.0.1 -t dmr-gateway/dynamic -m \"DynTG 2 " << talkgroup << "\"";
    return ::system(command.str().c_str()) == 0;
}

uint32_t getBmSourceId(uint32_t repeaterId)
{
    return repeaterId > 10000000U ? (repeaterId / 100U) : repeaterId;
}

uint32_t getBmApiTalkgroupSlot(const BMConfig& config)
{
    return bm::protocol::isRepeaterDeviceId(config.repeaterId) ? config.timeslot : 0U;
}

bool requestBmTalkgroupDelete(const std::filesystem::path& apiKeyPath, uint32_t deviceId, uint32_t slot, uint32_t talkgroup)
{
    std::ostringstream command;
    command
        << "/usr/bin/python3 - <<'PY'\n"
        << "from pathlib import Path\n"
        << "import requests\n"
        << "key = Path(r'" << apiKeyPath.string() << "').read_text(encoding='utf-8').strip()\n"
        << "headers = {'Authorization': f'Bearer {key}', 'Accept': 'application/json'}\n"
        << "response = requests.delete('https://api.brandmeister.network/v2/device/" << deviceId
        << "/talkgroup/" << slot << "/" << talkgroup << "', headers=headers, timeout=20)\n"
        << "response.raise_for_status()\n"
        << "PY";
    return ::system(command.str().c_str()) == 0;
}

bool requestBmDynamicDrop(const std::filesystem::path& apiKeyPath, uint32_t deviceId, uint32_t slot)
{
    std::ostringstream command;
    command
        << "/usr/bin/python3 - <<'PY'\n"
        << "from pathlib import Path\n"
        << "import requests\n"
        << "key = Path(r'" << apiKeyPath.string() << "').read_text(encoding='utf-8').strip()\n"
        << "headers = {'Authorization': f'Bearer {key}', 'Accept': 'application/json'}\n"
        << "base = 'https://api.brandmeister.network/v2/device/" << deviceId << "/action/'\n"
        << "for action in ('dropCallRoute', 'dropDynamicGroups'):\n"
        << "    response = requests.get(f\"{base}{action}/" << slot << "\", headers=headers, timeout=20)\n"
        << "    response.raise_for_status()\n"
        << "PY";
    return ::system(command.str().c_str()) == 0;
}

}

int main(int argc, char** argv)
{
    const std::string configPath = argc > 1 ? argv[1] : "quantarbridge.yml";

    std::signal(SIGINT, handleSignal);
    std::signal(SIGTERM, handleSignal);

    try {
        const AppConfig config = loadConfig(configPath);
        if (!::LogInitialise(config.logging.filePath, config.logging.fileRoot, config.logging.fileLevel, config.logging.displayLevel, false, config.logging.useSyslog)) {
            return 1;
        }

        ::LogInfoEx(LOG_HOST, "Starting quantarbridge using %s", configPath.c_str());
        ::LogInfoEx(LOG_HOST, "BM transport=%s callsign=%s lat=%s lon=%s height=%s location=%s description=%s softwareId=%s packageId=%s slot1=%u slot2=%u",
            config.bm.transport.c_str(),
            config.bm.callsign.c_str(), config.bm.latitude.c_str(), config.bm.longitude.c_str(), config.bm.height.c_str(),
            config.bm.location.c_str(), config.bm.description.c_str(), config.bm.softwareId.c_str(), config.bm.packageId.c_str(),
            config.bm.slot1 ? 1U : 0U, config.bm.slot2 ? 1U : 0U);

        network::Network fne(config.fne.address, config.fne.port, config.fne.localPort, config.fne.peerId, config.fne.password,
            true, config.fne.debug, true, false, false, false, true, true, false, true, false, false);
        fne.enable(true);
        fne.setMetadata("QUANTARBM", 0U, 0U, 0.0f, 12.5f, 0U, 0U, 0U, 0.0f, 0.0f, 0, "Quantar BrandMeister Bridge");

        if (config.fne.encrypted && config.fne.presharedKey.size() == 64U) {
            uint8_t key[32U];
            ::memset(key, 0x00U, sizeof(key));
            if (!decodeHexKey(config.fne.presharedKey, key, sizeof(key))) {
                throw std::runtime_error("Invalid FNE preshared key hex string");
            }
            fne.setPresharedKey(key);
        }

        if (!fne.open()) {
            ::LogError(LOG_HOST, "Failed to open FNE connection");
            return 1;
        }

        BMNetwork bm(config.bm);
        if (!bm.open()) {
            ::LogError(LOG_HOST, "Failed to open BrandMeister connection");
            return 1;
        }

        SmsSubsystem sms(config.sms);
        BmPacketDataReassembler bmPacketData;
        sms.setBmPacketDataWriter([&bm](uint32_t sourceRid, uint32_t targetRid, uint8_t slotNo, const std::vector<uint8_t>& ipPacket) {
            return bm.writePacketData(sourceRid, targetRid, slotNo, ipPacket);
        });
        if (sms.isEnabled() && !sms.open()) {
            ::LogError(LOG_HOST, "Failed to open SMS subsystem");
            bm.close();
            fne.close();
            return 1;
        }

        std::unordered_map<uint32_t, DynamicTalkgroupState> dynamicTalkgroups;
        std::unordered_map<uint32_t, std::chrono::steady_clock::time_point> rfPriorityTalkgroups;
        std::unordered_map<size_t, std::chrono::steady_clock::time_point> recentOutboundBmFrames;
        std::unordered_map<size_t, std::chrono::steady_clock::time_point> recentFneFrames;
        std::unordered_map<size_t, std::chrono::steady_clock::time_point> recentInboundBmFrames;
        std::array<std::optional<RecentHeader>, 2U> recentBmHeaders;
        std::array<std::optional<PendingTerminator>, 2U> pendingTerminators;
        std::array<std::optional<PendingTerminator>, 2U> pendingInboundTerminators;
        std::array<dmr::data::EmbeddedData, 2U> outboundMappedEmbeddedLC;
        std::array<dmr::data::EmbeddedData, 2U> inboundMappedEmbeddedLC;
        const auto dynamicTimeout = std::chrono::seconds(config.routing.dynamicTimeoutSeconds);
        const auto rfPriorityHoldoff = std::chrono::milliseconds(std::max<uint32_t>(250U, config.routing.rfPriorityHoldoffSeconds * 1000U));
        const auto dynamicStatePath = getDynamicStatePath(configPath);
        const auto dynamicActiveTriggerPath = getDynamicActiveTriggerPath(configPath);
        const auto dynamicExpiryTriggerPath = getDynamicExpiryTriggerPath(configPath);
        const auto bmApiKeyPath = getBmApiKeyPath(configPath);
        const std::string currentBootId = getCurrentBootId();
        const bool gatewayManagedRouting = config.bm.transport == "mmdvm-gateway";
        const uint32_t localBmBaseId = getBmSourceId(config.bm.repeaterId);
        const uint32_t bmApiSlot = getBmApiTalkgroupSlot(config.bm);
        const uint32_t primaryStaticTalkgroup = config.routing.staticTalkgroupOrder.empty() ? 0U : config.routing.staticTalkgroupOrder.front();
        AsyncTaskQueue bmApiTasks;
        auto lastTick = std::chrono::steady_clock::now();
        auto lastDynamicStateFlush = std::chrono::steady_clock::now();
        auto lastBmDynamicRepair = std::chrono::steady_clock::time_point::min();
        auto bmStartupCleanupAt = std::chrono::steady_clock::time_point::min();
        bool bmStartupCleanupDone = gatewayManagedRouting;
        bool dynamicStateDirty = false;

        DynamicWallClockMap dynamicTalkgroupsWall;
        std::unordered_map<uint32_t, std::chrono::steady_clock::time_point> restoredDynamicTalkgroups;
        if (gatewayManagedRouting) {
            loadDynamicTalkgroupState(dynamicStatePath, restoredDynamicTalkgroups, dynamicTalkgroupsWall, dynamicTimeout, currentBootId);
            for (const auto& entry : dynamicTalkgroupsWall) {
                DynamicTalkgroupState state;
                state.lastSeenWall = entry.second;
                state.lastSeenSteady = restoredDynamicTalkgroups[entry.first];
                dynamicTalkgroups.emplace(entry.first, state);
                ::LogInfoEx(LOG_HOST, "Restored dynamic TG %u from persisted state", entry.first);
            }
        } else {
            std::error_code ec;
            std::filesystem::remove(dynamicStatePath, ec);
        }

        auto sendToBM = [&](const dmr::data::NetData& data) {
            if (!bm.isConnected()) {
                return;
            }

            dmr::data::NetData mapped = data;
            const uint32_t localSlotNo = mapped.getSlotNo();
            mapped.setSlotNo(config.bm.timeslot);
            if (mapped.getFLCO() == FLCO::GROUP) {
                const auto mapping = config.routing.p25ToBmTalkgroups.find(mapped.getDstId());
                if (mapping != config.routing.p25ToBmTalkgroups.end()) {
                    const uint32_t oldDstId = mapped.getDstId();
                    ::LogInfoEx(LOG_HOST, "Mapping P25/FNE TG %u to BrandMeister TG %u",
                        oldDstId, mapping->second);
                    mapped.setDstId(mapping->second);
                    rewriteEmbeddedVoiceLinkControl(mapped, oldDstId, mapping->second, "P25/FNE->BrandMeister");
                    const uint32_t mappedSlotIndex = localSlotNo > 0U ? localSlotNo - 1U : 0U;
                    if (mappedSlotIndex < outboundMappedEmbeddedLC.size()) {
                        rewriteGeneratedEmbeddedVoiceLinkControl(mapped, outboundMappedEmbeddedLC[mappedSlotIndex],
                            oldDstId, mapping->second, "P25/FNE->BrandMeister");
                    }
                }
            }

            ::LogDebug(LOG_HOST, "Forwarding FNE DMR to BrandMeister srcId=%u dstId=%u slot=%u",
                mapped.getSrcId(), mapped.getDstId(), mapped.getSlotNo());

            recentOutboundBmFrames[dmrFrameSignature(mapped)] = std::chrono::steady_clock::now() + kBmEchoSuppressWindow;

            bm.write(mapped);
        };

        auto flushPendingTerminators = [&](const std::chrono::steady_clock::time_point& currentTime, bool force = false) {
            for (auto& pending : pendingTerminators) {
                if (!pending.has_value()) {
                    continue;
                }

                if (!force && currentTime < pending->expiresAt) {
                    continue;
                }

                ::LogInfoEx(LOG_HOST, "Flushing delayed DMR terminator to BrandMeister srcId=%u dstId=%u slot=%u",
                    pending->srcId, pending->dstId, pending->data.getSlotNo());
                sendToBM(pending->data);
                pending.reset();
            }
        };

        auto flushPendingInboundTerminators = [&](const std::chrono::steady_clock::time_point& currentTime, bool force = false) {
            for (auto& pending : pendingInboundTerminators) {
                if (!pending.has_value()) {
                    continue;
                }

                if (!force && currentTime < pending->expiresAt) {
                    continue;
                }

                ::LogInfoEx(LOG_HOST, "Flushing delayed BM DMR terminator to FNE srcId=%u dstId=%u slot=%u",
                    pending->srcId, pending->dstId, pending->data.getSlotNo());
                fne.writeDMR(pending->data);
                pending.reset();
            }
        };

        auto sendBmDisconnect = [&](uint32_t srcId) {
            if (!bm.isConnected()) {
                return;
            }

            const uint32_t effectiveSrcId = srcId != 0U ? srcId :
                (localBmBaseId != 0U ? localBmBaseId : config.bm.repeaterId);

            auto sendDisconnectCall = [&](FLCO::E flco, uint8_t headerSeq, uint8_t termSeq, const char* kind) {
                dmr::data::NetData disconnectHeader;
                disconnectHeader.setSeqNo(headerSeq);
                disconnectHeader.setSrcId(effectiveSrcId);
                disconnectHeader.setDstId(config.routing.disconnectTalkgroup);
                disconnectHeader.setSlotNo(config.bm.timeslot);
                disconnectHeader.setFLCO(flco);
                disconnectHeader.setDataType(DataType::VOICE_LC_HEADER);
                disconnectHeader.setN(0U);
                disconnectHeader.setBER(0U);
                disconnectHeader.setRSSI(0U);
                uint8_t payload[33U];
                ::memset(payload, 0x00U, sizeof(payload));
                disconnectHeader.setData(payload);

                dmr::data::NetData disconnectTerminator = disconnectHeader;
                disconnectTerminator.setSeqNo(termSeq);
                disconnectTerminator.setDataType(DataType::TERMINATOR_WITH_LC);

                ::LogInfoEx(LOG_HOST, "Sending BM unlink (%s) on TG %u using srcId=%u after dynamic route expiry",
                    kind, config.routing.disconnectTalkgroup, effectiveSrcId);
                bm.write(disconnectHeader);
                bm.write(disconnectTerminator);
            };

            for (uint8_t burst = 0U; burst < 3U; ++burst) {
                sendDisconnectCall(FLCO::GROUP, static_cast<uint8_t>(burst * 2U), static_cast<uint8_t>(burst * 2U + 1U), "group");
                std::this_thread::sleep_for(std::chrono::milliseconds(150));
            }
            sendDisconnectCall(FLCO::PRIVATE, 6U, 7U, "private");
        };

        while (g_running.load()) {
            const auto now = std::chrono::steady_clock::now();
            const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - lastTick);
            lastTick = now;
            const uint32_t tickMs = elapsed.count() == 0 ? 1U : static_cast<uint32_t>(elapsed.count());

            fne.clock(tickMs);
            bm.clock(tickMs);
            sms.clock();
            flushPendingTerminators(now);
            flushPendingInboundTerminators(now);

            if (!gatewayManagedRouting && bm.isConnected()) {
                if (!bmStartupCleanupDone && bmStartupCleanupAt == std::chrono::steady_clock::time_point::min()) {
                    bmStartupCleanupAt = now + kBmStartupCleanupDelay;
                }
                if (!bmStartupCleanupDone && now >= bmStartupCleanupAt && std::filesystem::exists(bmApiKeyPath)) {
                    const bool queued = bmApiTasks.enqueue([bmApiKeyPath, deviceId = config.bm.repeaterId, bmApiSlot]() {
                        if (requestBmDynamicDrop(bmApiKeyPath, deviceId, bmApiSlot)) {
                            ::LogInfoEx(LOG_HOST, "Requested BrandMeister startup dynamic cleanup after direct BM connect");
                        } else {
                            ::LogWarning(LOG_HOST, "Failed BrandMeister startup dynamic cleanup after direct BM connect");
                        }
                    });
                    if (!queued) {
                        ::LogWarning(LOG_HOST, "BrandMeister startup cleanup queue is full");
                    }
                    bmStartupCleanupDone = true;
                    lastBmDynamicRepair = now;
                }
            } else if (!gatewayManagedRouting && !bm.isConnected()) {
                bmStartupCleanupAt = std::chrono::steady_clock::time_point::min();
                bmStartupCleanupDone = false;
            }

            for (auto it = dynamicTalkgroups.begin(); it != dynamicTalkgroups.end();) {
                if ((now - it->second.lastSeenSteady) > dynamicTimeout) {
                    if (!it->second.expiredPendingBmRelease) {
                        ::LogInfoEx(LOG_HOST, "Dynamic TG %u expired locally, waiting for BM release", it->first);
                        it->second.expiredPendingBmRelease = true;
                        dynamicTalkgroupsWall.erase(it->first);
                        if (!gatewayManagedRouting && !it->second.bmReleaseRequested) {
                            if (std::filesystem::exists(bmApiKeyPath)) {
                                const uint32_t talkgroup = it->first;
                                const bool queued = bmApiTasks.enqueue([bmApiKeyPath, deviceId = config.bm.repeaterId, bmApiSlot, talkgroup]() {
                                    const bool deleted = requestBmTalkgroupDelete(bmApiKeyPath, deviceId, bmApiSlot, talkgroup);
                                    const bool dropped = requestBmDynamicDrop(bmApiKeyPath, deviceId, bmApiSlot);
                                    if (!deleted || !dropped) {
                                        ::LogWarning(LOG_HOST, "Failed asynchronous BrandMeister release for TG %u", talkgroup);
                                    }
                                });
                                if (!queued) {
                                    ::LogWarning(LOG_HOST, "BrandMeister release queue is full for TG %u", talkgroup);
                                }
                            }
                            it->second.bmReleaseRequested = true;
                        }
                        writeDynamicExpiryTrigger(dynamicExpiryTriggerPath, it->first);
                        dynamicStateDirty = true;
                        ++it;
                    } else if (it->second.lastBmSeenSteady != std::chrono::steady_clock::time_point::min() &&
                        (now - it->second.lastBmSeenSteady) <= kBmDynamicReleaseGrace) {
                        ++it;
                    } else {
                        ::LogInfoEx(LOG_HOST, "Dynamic TG %u fully released after BM idle", it->first);
                        if (gatewayManagedRouting) {
                            if (primaryStaticTalkgroup != 0U) {
                                publishGatewayDynTG(primaryStaticTalkgroup);
                            }
                        }
                        dynamicStateDirty = true;
                        it = dynamicTalkgroups.erase(it);
                    }
                } else {
                    ++it;
                }
            }

            for (auto it = rfPriorityTalkgroups.begin(); it != rfPriorityTalkgroups.end();) {
                if (now >= it->second) {
                    it = rfPriorityTalkgroups.erase(it);
                } else {
                    ++it;
                }
            }

            for (auto it = recentOutboundBmFrames.begin(); it != recentOutboundBmFrames.end();) {
                if (now >= it->second) {
                    it = recentOutboundBmFrames.erase(it);
                } else {
                    ++it;
                }
            }

            for (auto it = recentFneFrames.begin(); it != recentFneFrames.end();) {
                if (now >= it->second) {
                    it = recentFneFrames.erase(it);
                } else {
                    ++it;
                }
            }

            for (auto it = recentInboundBmFrames.begin(); it != recentInboundBmFrames.end();) {
                if (now >= it->second) {
                    it = recentInboundBmFrames.erase(it);
                } else {
                    ++it;
                }
            }

            if (gatewayManagedRouting && dynamicStateDirty && (now - lastDynamicStateFlush) >= kDynamicStateFlushInterval) {
                saveDynamicTalkgroupState(dynamicStatePath, dynamicTalkgroupsWall, currentBootId);
                lastDynamicStateFlush = now;
                dynamicStateDirty = false;
            }

            for (uint32_t fnePackets = 0U; fnePackets < kMaxFnePacketsPerLoop && fne.hasDMRData(); ++fnePackets) {
                bool ok = false;
                uint32_t length = 0U;
                auto raw = fne.readDMR(ok, length);
                if (!ok || raw == nullptr) {
                    break;
                }

                auto data = parseFNERawDMR(raw.get(), length);
                const bool isPrivate = data.getFLCO() != FLCO::GROUP;
                const uint32_t slotNo = data.getSlotNo();
                const uint32_t slotIndex = slotNo > 0U ? slotNo - 1U : 0U;
                const size_t frameSig = dmrFrameSignature(data);
                ::LogDebug(LOG_HOST, "FNE->BM DMR frame srcId=%u dstId=%u slot=%u type=%u private=%u len=%u",
                    data.getSrcId(), data.getDstId(), slotNo, static_cast<uint32_t>(data.getDataType()), isPrivate ? 1U : 0U, length);

                if (!gatewayManagedRouting && !config.routing.allowPrivateCalls && isPrivate && !isDmrPacketDataFrame(data)) {
                    ::LogInfoEx(LOG_HOST, "Dropping private DMR voice/control frame on uplink srcId=%u dstId=%u slot=%u type=%u",
                        data.getSrcId(), data.getDstId(), slotNo, static_cast<uint32_t>(data.getDataType()));
                    continue;
                }

                if (config.bm.repeaterId > 10000000U && data.getSrcId() == config.bm.repeaterId) {
                    ::LogInfoEx(LOG_HOST, "Dropping local BM/P25 loopback on uplink srcId=%u dstId=%u slot=%u repeaterId=%u",
                        data.getSrcId(), data.getDstId(), slotNo, config.bm.repeaterId);
                    continue;
                }

                const bool dedupeFneFrame =
                    data.getDataType() == DataType::VOICE_LC_HEADER ||
                    data.getDataType() == DataType::TERMINATOR_WITH_LC;
                if (dedupeFneFrame) {
                    const auto duplicate = recentFneFrames.find(frameSig);
                    if (duplicate != recentFneFrames.end() && now < duplicate->second) {
                        ::LogInfoEx(LOG_HOST, "Dropping duplicate FNE DMR control frame on uplink srcId=%u dstId=%u slot=%u",
                            data.getSrcId(), data.getDstId(), slotNo);
                        continue;
                    }
                    recentFneFrames[frameSig] = now + kFneDuplicateSuppressWindow;
                }

                // Downlink audio forwarded to the local FNE peer can reappear immediately as an
                // uplink-looking frame when the downstream P25/DFSI side emits hold/pad traffic.
                // Drop exact near-term reflections so they do not loop back to BrandMeister.
                const auto reflectedInbound = recentInboundBmFrames.find(frameSig);
                if (reflectedInbound != recentInboundBmFrames.end() && now < reflectedInbound->second) {
                    ::LogInfoEx(LOG_HOST, "Dropping reflected BM->FNE loopback on uplink srcId=%u dstId=%u slot=%u",
                        data.getSrcId(), data.getDstId(), slotNo);
                    continue;
                }

            if (false && slotIndex < pendingTerminators.size() && pendingTerminators[slotIndex].has_value()) {
                    const auto& pending = pendingTerminators[slotIndex].value();
                    const bool sameCall = pending.srcId == data.getSrcId() &&
                        pending.dstId == data.getDstId() &&
                        pending.flco == data.getFLCO() &&
                        now < pending.expiresAt;

                    if (sameCall) {
                        if (data.getDataType() == DataType::VOICE_LC_HEADER) {
                            ::LogInfoEx(LOG_HOST, "Merging short DMR restart for srcId=%u dstId=%u slot=%u by suppressing repeated header",
                                data.getSrcId(), data.getDstId(), slotNo);
                            pendingTerminators[slotIndex].reset();
                            continue;
                        }

                        ::LogInfoEx(LOG_HOST, "Merging short DMR restart for srcId=%u dstId=%u slot=%u by suppressing delayed terminator",
                            data.getSrcId(), data.getDstId(), slotNo);
                        pendingTerminators[slotIndex].reset();
                    } else {
                        flushPendingTerminators(now, true);
                    }
                }

                if (!isPrivate) {
                    const uint32_t dstId = data.getDstId();
                    uint32_t bmRouteDstId = dstId;
                    const auto uplinkMapping = config.routing.p25ToBmTalkgroups.find(dstId);
                    if (uplinkMapping != config.routing.p25ToBmTalkgroups.end()) {
                        bmRouteDstId = uplinkMapping->second;
                    }
                    if (dstId == config.routing.disconnectTalkgroup) {
                        std::vector<uint32_t> activeDynamicTalkgroups;
                        activeDynamicTalkgroups.reserve(dynamicTalkgroups.size());
                        for (const auto& entry : dynamicTalkgroups) {
                            activeDynamicTalkgroups.push_back(entry.first);
                        }

                        dynamicTalkgroups.clear();
                        dynamicTalkgroupsWall.clear();
                        rfPriorityTalkgroups.clear();
                        if (gatewayManagedRouting) {
                            if (primaryStaticTalkgroup != 0U) {
                                publishGatewayDynTG(primaryStaticTalkgroup);
                            }
                        } else {
                            if (bm.isConnected() && std::filesystem::exists(bmApiKeyPath)) {
                                const bool queued = bmApiTasks.enqueue([bmApiKeyPath, deviceId = config.bm.repeaterId, bmApiSlot, activeDynamicTalkgroups]() {
                                    bool ok = true;
                                    for (uint32_t dynamicTalkgroup : activeDynamicTalkgroups) {
                                        ok = requestBmTalkgroupDelete(bmApiKeyPath, deviceId, bmApiSlot, dynamicTalkgroup) && ok;
                                    }
                                    ok = requestBmDynamicDrop(bmApiKeyPath, deviceId, bmApiSlot) && ok;
                                    if (!ok) {
                                        ::LogWarning(LOG_HOST, "Failed asynchronous BrandMeister disconnect cleanup");
                                    }
                                });
                                if (!queued) {
                                    ::LogWarning(LOG_HOST, "BrandMeister disconnect cleanup queue is full");
                                }
                                lastBmDynamicRepair = now;
                            }
                            sendBmDisconnect(data.getSrcId());
                            if (bm.isConnected()) {
                                ::LogInfoEx(LOG_HOST, "Reconnecting BrandMeister session after local disconnect TG to restore static routing");
                                bm.close();
                                bm.open();
                                bmStartupCleanupAt = std::chrono::steady_clock::time_point::min();
                                bmStartupCleanupDone = false;
                            }
                        }
                        dynamicStateDirty = true;
                        ::LogInfoEx(LOG_HOST, "Received disconnect TG %u from RF side, clearing dynamic TG state", dstId);
                        continue;
                    }

                    if (config.routing.staticTalkgroups.count(bmRouteDstId) == 0U) {
                        const bool isNewDynamicTalkgroup = dynamicTalkgroups.find(bmRouteDstId) == dynamicTalkgroups.end();
                        auto& state = dynamicTalkgroups[bmRouteDstId];
                        state.lastSeenSteady = now;
                        state.lastSeenWall = std::chrono::system_clock::now();
                        state.lastSrcId = data.getSrcId();
                        state.expiredPendingBmRelease = false;
                        state.bmReleaseRequested = false;
                        dynamicTalkgroupsWall[bmRouteDstId] = state.lastSeenWall;
                        if (isNewDynamicTalkgroup && gatewayManagedRouting) {
                            writeDynamicActiveTrigger(dynamicActiveTriggerPath, bmRouteDstId);
                            publishGatewayDynTG(bmRouteDstId);
                        }
                        dynamicStateDirty = true;
                        ::LogInfoEx(LOG_HOST, "Updated dynamic TG %u from RF activity", bmRouteDstId);
                    }
                    if (data.getDataType() != DataType::TERMINATOR_WITH_LC) {
                        rfPriorityTalkgroups[dstId] = now + rfPriorityHoldoff;
                    }
                }

                if (false && data.getDataType() == DataType::TERMINATOR_WITH_LC && slotIndex < pendingTerminators.size()) {
                    PendingTerminator pending;
                    pending.data = data;
                    pending.srcId = data.getSrcId();
                    pending.dstId = data.getDstId();
                    pending.flco = data.getFLCO();
                    pending.expiresAt = now + kCallMergeWindow;
                    pendingTerminators[slotIndex] = pending;
                    if (!isPrivate) {
                        const auto holdUntil = now + rfPriorityHoldoff;
                        auto& rfExpiry = rfPriorityTalkgroups[data.getDstId()];
                        if (rfExpiry < holdUntil) {
                            rfExpiry = holdUntil;
                        }
                    }
                    ::LogInfoEx(LOG_HOST, "Delaying DMR terminator for srcId=%u dstId=%u slot=%u to smooth short call gaps",
                        data.getSrcId(), data.getDstId(), slotNo);
                    continue;
                }

                sendToBM(data);
            }

            dmr::data::NetData inbound;
            for (uint32_t inboundPackets = 0U; inboundPackets < kMaxBmPacketsPerLoop && bm.read(inbound); ++inboundPackets) {
                const bool isPrivate = inbound.getFLCO() != FLCO::GROUP;
                bool mappedInboundTalkgroup = false;
                if (gatewayManagedRouting && !isPrivate && inbound.getSlotNo() == 2U && inbound.getDstId() == 9U) {
                    const uint32_t dynamicTalkgroup = selectActiveDynamicTalkgroup(dynamicTalkgroups, now, dynamicTimeout);
                    const uint32_t effectiveTalkgroup = dynamicTalkgroup != 0U ? dynamicTalkgroup : primaryStaticTalkgroup;
                    if (effectiveTalkgroup != 0U) {
                        ::LogInfoEx(LOG_HOST, "Remapping BM gateway TG9 downlink to TG %u for local routing", effectiveTalkgroup);
                        inbound.setDstId(effectiveTalkgroup);
                    }
                }
                if (!isPrivate) {
                    const auto mapping = config.routing.bmToP25Talkgroups.find(inbound.getDstId());
                    if (mapping != config.routing.bmToP25Talkgroups.end()) {
                        const uint32_t oldDstId = inbound.getDstId();
                        ::LogInfoEx(LOG_HOST, "Mapping BrandMeister TG %u to P25/FNE TG %u",
                            oldDstId, mapping->second);
                        inbound.setDstId(mapping->second);
                        rewriteEmbeddedVoiceLinkControl(inbound, oldDstId, mapping->second, "BrandMeister->P25/FNE");
                        const uint32_t mappedSlotIndex = inbound.getSlotNo() > 0U ? inbound.getSlotNo() - 1U : 0U;
                        if (mappedSlotIndex < inboundMappedEmbeddedLC.size()) {
                            rewriteGeneratedEmbeddedVoiceLinkControl(inbound, inboundMappedEmbeddedLC[mappedSlotIndex],
                                oldDstId, mapping->second, "BrandMeister->P25/FNE");
                        }
                        mappedInboundTalkgroup = true;
                    }
                }
                const size_t inboundSig = dmrFrameSignature(inbound);
                const uint32_t inboundSlotNo = inbound.getSlotNo();
                const uint32_t inboundSlotIndex = inboundSlotNo > 0U ? inboundSlotNo - 1U : 0U;
                ::LogInfoEx(LOG_HOST, "BM->FNE DMR frame srcId=%u dstId=%u slot=%u type=%u private=%u",
                    inbound.getSrcId(), inbound.getDstId(), inboundSlotNo, static_cast<uint32_t>(inbound.getDataType()), isPrivate ? 1U : 0U);
                if (isPrivate && isDmrPacketDataFrame(inbound)) {
                    uint8_t payload[33U];
                    ::memset(payload, 0x00U, sizeof(payload));
                    inbound.getData(payload);
                    ::LogInfoEx(LOG_HOST, "BM private packet-data payload srcId=%u dstId=%u type=%u hex=%s",
                        inbound.getSrcId(), inbound.getDstId(), static_cast<uint32_t>(inbound.getDataType()),
                        bytesToHex(payload, sizeof(payload)).c_str());
                    if (inbound.getDataType() == DataType::DATA_HEADER) {
                        dmr::data::DataHeader header;
                        if (header.decode(payload)) {
                            ::LogInfoEx(LOG_HOST,
                                "BM private DATA_HEADER decoded dpf=%u ack=%u sap=%u full=%u btf=%u pad=%u pktLen=%u fsn=%u ns=%u srcId=%u dstId=%u group=%u rspClass=%u rspType=%u rspStatus=%u",
                                static_cast<uint32_t>(header.getDPF()), header.getA() ? 1U : 0U,
                                static_cast<uint32_t>(header.getSAP()), header.getFullMesage() ? 1U : 0U,
                                header.getBlocksToFollow(), static_cast<uint32_t>(header.getPadLength()),
                                header.getPacketLength(inbound.getDataType()), static_cast<uint32_t>(header.getFSN()),
                                static_cast<uint32_t>(header.getNs()), header.getSrcId(), header.getDstId(),
                                header.getGI() ? 1U : 0U, static_cast<uint32_t>(header.getResponseClass()),
                                static_cast<uint32_t>(header.getResponseType()), static_cast<uint32_t>(header.getResponseStatus()));
                        } else {
                            ::LogWarning(LOG_HOST, "BM private DATA_HEADER decode failed srcId=%u dstId=%u",
                                inbound.getSrcId(), inbound.getDstId());
                        }
                    }
                }

                if (inboundSlotIndex < pendingInboundTerminators.size() && pendingInboundTerminators[inboundSlotIndex].has_value()) {
                    const auto& pending = pendingInboundTerminators[inboundSlotIndex].value();
                    const bool sameCall = pending.srcId == inbound.getSrcId() &&
                        pending.dstId == inbound.getDstId() &&
                        pending.flco == inbound.getFLCO() &&
                        now < pending.expiresAt;

                    if (sameCall) {
                        if (inbound.getDataType() == DataType::VOICE_LC_HEADER) {
                            ::LogInfoEx(LOG_HOST, "Merging short BM restart for srcId=%u dstId=%u slot=%u by suppressing repeated header",
                                inbound.getSrcId(), inbound.getDstId(), inboundSlotNo);
                            pendingInboundTerminators[inboundSlotIndex].reset();
                            continue;
                        }

                        ::LogInfoEx(LOG_HOST, "Merging short BM restart for srcId=%u dstId=%u slot=%u by suppressing delayed BM terminator",
                            inbound.getSrcId(), inbound.getDstId(), inboundSlotNo);
                        pendingInboundTerminators[inboundSlotIndex].reset();
                    } else {
                        flushPendingInboundTerminators(now, true);
                    }
                }

                const auto inboundDuplicate = recentInboundBmFrames.find(inboundSig);
                if (inboundDuplicate != recentInboundBmFrames.end() && now < inboundDuplicate->second) {
                    ::LogInfoEx(LOG_HOST, "Dropping duplicate BM DMR frame srcId=%u dstId=%u slot=%u",
                        inbound.getSrcId(), inbound.getDstId(), inboundSlotNo);
                    continue;
                }
                recentInboundBmFrames[inboundSig] = now + kBmDuplicateSuppressWindow;

                if (isPrivate && isDmrPacketDataFrame(inbound)) {
                    auto packetDataResult = bmPacketData.push(inbound, now);
                    if (packetDataResult.packet.has_value()) {
                        const auto& packet = packetDataResult.packet.value();
                        ::LogInfoEx(LOG_HOST,
                            "BM private packet-data reassembled srcId=%u dstId=%u slot=%u ns=%u ipLen=%u ipHex=%s",
                            packet.sourceRid, packet.targetRid, packet.slotNo, packet.sequenceNo,
                            static_cast<uint32_t>(packet.bytes.size()),
                            bytesToHex(packet.bytes.data(), packet.bytes.size()).c_str());
                        if (!sms.handleBrandmeisterPacketData(packet.sourceRid, packet.targetRid,
                            packet.slotNo, packet.bytes)) {
                            ::LogWarning(LOG_HOST,
                                "BM private packet-data was complete but not recognized as a routable TMS reply, srcId=%u dstId=%u",
                                packet.sourceRid, packet.targetRid);
                        }
                    }
                    if (packetDataResult.consumed) {
                        continue;
                    }
                }

                if (inbound.getDataType() == DataType::VOICE_LC_HEADER && inboundSlotIndex < recentBmHeaders.size()) {
                    const auto& recentHeader = recentBmHeaders[inboundSlotIndex];
                    if (recentHeader.has_value() &&
                        now < recentHeader->expiresAt &&
                        recentHeader->srcId == inbound.getSrcId() &&
                        recentHeader->dstId == inbound.getDstId() &&
                        recentHeader->slotNo == inboundSlotNo &&
                        recentHeader->flco == inbound.getFLCO()) {
                        ::LogInfoEx(LOG_HOST, "Dropping repeated BM DMR header srcId=%u dstId=%u slot=%u to keep one stable downlink stream",
                            inbound.getSrcId(), inbound.getDstId(), inboundSlotNo);
                        continue;
                    }

                    RecentHeader header;
                    header.srcId = inbound.getSrcId();
                    header.dstId = inbound.getDstId();
                    header.slotNo = inboundSlotNo;
                    header.flco = inbound.getFLCO();
                    header.expiresAt = now + kBmHeaderSuppressWindow;
                    recentBmHeaders[inboundSlotIndex] = header;
                } else if (inbound.getDataType() == DataType::TERMINATOR_WITH_LC && inboundSlotIndex < recentBmHeaders.size()) {
                    recentBmHeaders[inboundSlotIndex].reset();
                }

                if (!gatewayManagedRouting && !config.routing.allowPrivateCalls && isPrivate && !isDmrPacketDataFrame(inbound)) {
                    ::LogInfoEx(LOG_HOST, "Dropping private BM voice/control frame on downlink srcId=%u dstId=%u slot=%u type=%u",
                        inbound.getSrcId(), inbound.getDstId(), inboundSlotNo, static_cast<uint32_t>(inbound.getDataType()));
                    continue;
                }

                bool permitted = isPrivate;
                if (!isPrivate) {
                    const uint32_t dstId = inbound.getDstId();
                    permitted = mappedInboundTalkgroup || config.routing.staticTalkgroups.count(dstId) > 0U;
                    if (!permitted) {
                        const auto entry = dynamicTalkgroups.find(dstId);
                        permitted = entry != dynamicTalkgroups.end() &&
                            ((!entry->second.expiredPendingBmRelease && (now - entry->second.lastSeenSteady) <= dynamicTimeout) ||
                             (entry->second.expiredPendingBmRelease &&
                              (entry->second.lastBmSeenSteady == std::chrono::steady_clock::time_point::min() ||
                               (now - entry->second.lastBmSeenSteady) <= kBmDynamicReleaseGrace)));
                    }
                }

                if (!permitted) {
                    ::LogInfoEx(LOG_HOST, "Dropping BM DMR frame for dstId=%u, no static or dynamic route", inbound.getDstId());
                    if (!gatewayManagedRouting &&
                        inbound.getFLCO() == FLCO::GROUP &&
                        inbound.getSlotNo() == 2U &&
                        inbound.getDstId() >= 90U &&
                        (now - lastBmDynamicRepair) >= kBmApiRepairCooldown) {
                        ::LogInfoEx(LOG_HOST, "Attempting BrandMeister dynamic route repair for unexpected TG %u downlink using %s",
                            inbound.getDstId(), bmApiKeyPath.c_str());
                        const uint32_t unexpectedTalkgroup = inbound.getDstId();
                        const uint32_t repairSlot = bmApiSlot;
                        const bool queued = bmApiTasks.enqueue([bmApiKeyPath, deviceId = config.bm.repeaterId, repairSlot, unexpectedTalkgroup]() {
                            if (requestBmDynamicDrop(bmApiKeyPath, deviceId, repairSlot)) {
                                ::LogInfoEx(LOG_HOST, "Requested BrandMeister dynamic route drop after unexpected TG %u downlink", unexpectedTalkgroup);
                            } else {
                                ::LogWarning(LOG_HOST, "Failed to request BrandMeister dynamic route drop for unexpected TG %u downlink", unexpectedTalkgroup);
                            }
                        });
                        if (!queued) {
                            ::LogWarning(LOG_HOST, "BrandMeister dynamic repair queue is full for TG %u", unexpectedTalkgroup);
                        }
                        lastBmDynamicRepair = now;
                    }
                    continue;
                }

                if (!isPrivate) {
                    auto entry = dynamicTalkgroups.find(inbound.getDstId());
                    if (entry != dynamicTalkgroups.end()) {
                        entry->second.lastBmSeenSteady = now;
                    }
                }

                if (!isPrivate) {
                    const auto echo = recentOutboundBmFrames.find(dmrFrameSignature(inbound));
                    if (echo != recentOutboundBmFrames.end() && now < echo->second) {
                        ::LogInfoEx(LOG_HOST, "Dropping reflected BM echo on TG %u for srcId=%u after local uplink",
                            inbound.getDstId(), inbound.getSrcId());
                        continue;
                    }
                }

                if (!isPrivate) {
                    const auto rfPriority = rfPriorityTalkgroups.find(inbound.getDstId());
                    if (rfPriority != rfPriorityTalkgroups.end() && now < rfPriority->second) {
                        ::LogInfoEx(LOG_HOST, "Suppressing BM->FNE traffic on TG %u while local RF has priority", inbound.getDstId());
                        continue;
                    }
                }

                // The downstream DMR->P25 transcode path is wired for TS2. Accept BM on either slot,
                // but normalize it before handing it to the local FNE peer.
                if (inbound.getSlotNo() != 2U) {
                    ::LogInfoEx(LOG_HOST, "Remapping BrandMeister DMR frame from slot %u to slot 2 for local routing", inbound.getSlotNo());
                    inbound.setSlotNo(2U);
                }

                if (inbound.getDataType() == DataType::TERMINATOR_WITH_LC && inboundSlotIndex < pendingInboundTerminators.size()) {
                    PendingTerminator pending;
                    pending.data = inbound;
                    pending.srcId = inbound.getSrcId();
                    pending.dstId = inbound.getDstId();
                    pending.flco = inbound.getFLCO();
                    pending.expiresAt = now + kCallMergeWindow;
                    pendingInboundTerminators[inboundSlotIndex] = pending;
                    ::LogInfoEx(LOG_HOST, "Delaying BM DMR terminator for srcId=%u dstId=%u slot=%u to smooth short downlink gaps",
                        inbound.getSrcId(), inbound.getDstId(), inbound.getSlotNo());
                    continue;
                }

                if (inbound.getDataType() == DataType::VOICE_LC_HEADER || inbound.getDataType() == DataType::TERMINATOR_WITH_LC) {
                    ::LogInfoEx(LOG_HOST, "Forwarding BrandMeister DMR to FNE srcId=%u dstId=%u slot=%u",
                        inbound.getSrcId(), inbound.getDstId(), inbound.getSlotNo());
                } else {
                    ::LogDebug(LOG_HOST, "Forwarding BrandMeister DMR to FNE srcId=%u dstId=%u slot=%u",
                        inbound.getSrcId(), inbound.getDstId(), inbound.getSlotNo());
                }
                fne.writeDMR(inbound);
            }

            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }

        flushPendingTerminators(std::chrono::steady_clock::now(), true);
        flushPendingInboundTerminators(std::chrono::steady_clock::now(), true);
        if (gatewayManagedRouting && dynamicStateDirty) {
            saveDynamicTalkgroupState(dynamicStatePath, dynamicTalkgroupsWall, currentBootId);
        }
        bmApiTasks.shutdown();
        sms.close();
        bm.close();
        fne.close();
        ::LogFinalise();
        return 0;
    } catch (const std::exception& ex) {
        ::LogError(LOG_HOST, "Fatal error: %s", ex.what());
        ::LogFinalise();
        return 1;
    }
}
