#include "AppConfig.h"

#include <yaml-cpp/yaml.h>

#include <filesystem>
#include <stdexcept>

namespace {
template <typename T>
void assignIfPresent(const YAML::Node& node, const char* key, T& target)
{
    if (node[key]) {
        target = node[key].as<T>();
    }
}
}

AppConfig loadConfig(const std::string& path)
{
    const YAML::Node root = YAML::LoadFile(path);
    AppConfig config;

    const YAML::Node logging = root["logging"];
    if (logging) {
        assignIfPresent(logging, "filePath", config.logging.filePath);
        assignIfPresent(logging, "fileRoot", config.logging.fileRoot);
        assignIfPresent(logging, "fileLevel", config.logging.fileLevel);
        assignIfPresent(logging, "displayLevel", config.logging.displayLevel);
        assignIfPresent(logging, "useSyslog", config.logging.useSyslog);
    }

    const YAML::Node fne = root["fne"];
    if (!fne) {
        throw std::runtime_error("Missing required 'fne' section");
    }
    assignIfPresent(fne, "peerId", config.fne.peerId);
    assignIfPresent(fne, "address", config.fne.address);
    assignIfPresent(fne, "port", config.fne.port);
    assignIfPresent(fne, "localPort", config.fne.localPort);
    assignIfPresent(fne, "password", config.fne.password);
    assignIfPresent(fne, "encrypted", config.fne.encrypted);
    assignIfPresent(fne, "presharedKey", config.fne.presharedKey);
    assignIfPresent(fne, "debug", config.fne.debug);

    const YAML::Node bm = root["brandmeister"];
    if (!bm) {
        throw std::runtime_error("Missing required 'brandmeister' section");
    }
    assignIfPresent(bm, "repeaterId", config.bm.repeaterId);
    assignIfPresent(bm, "password", config.bm.password);
    assignIfPresent(bm, "transport", config.bm.transport);
    assignIfPresent(bm, "voiceEnabled", config.bm.voiceEnabled);
    assignIfPresent(bm, "address", config.bm.address);
    assignIfPresent(bm, "port", config.bm.port);
    assignIfPresent(bm, "localPort", config.bm.localPort);
    assignIfPresent(bm, "callsign", config.bm.callsign);
    assignIfPresent(bm, "rxFrequency", config.bm.rxFrequency);
    assignIfPresent(bm, "txFrequency", config.bm.txFrequency);
    assignIfPresent(bm, "power", config.bm.power);
    assignIfPresent(bm, "colorCode", config.bm.colorCode);
    assignIfPresent(bm, "latitude", config.bm.latitude);
    assignIfPresent(bm, "longitude", config.bm.longitude);
    assignIfPresent(bm, "height", config.bm.height);
    assignIfPresent(bm, "location", config.bm.location);
    assignIfPresent(bm, "description", config.bm.description);
    assignIfPresent(bm, "url", config.bm.url);
    assignIfPresent(bm, "softwareId", config.bm.softwareId);
    assignIfPresent(bm, "packageId", config.bm.packageId);
    assignIfPresent(bm, "slot1", config.bm.slot1);
    assignIfPresent(bm, "slot2", config.bm.slot2);
    if (bm["timeslot"]) {
        config.bm.timeslot = bm["timeslot"].as<uint8_t>();
        if (config.bm.timeslot != 1U && config.bm.timeslot != 2U) {
            throw std::runtime_error("brandmeister.timeslot must be 1 or 2");
        }
        config.bm.slot1 = config.bm.timeslot == 1U;
        config.bm.slot2 = config.bm.timeslot == 2U;
    } else if (config.bm.slot1 != config.bm.slot2) {
        config.bm.timeslot = config.bm.slot1 ? 1U : 2U;
    } else {
        throw std::runtime_error("brandmeister must enable exactly one timeslot");
    }
    assignIfPresent(bm, "options", config.bm.options);
    assignIfPresent(bm, "debug", config.bm.debug);

    if (config.bm.transport != "mmdvm-gateway" && config.bm.password.empty()) {
        throw std::runtime_error("brandmeister.password must not be empty");
    }

    const YAML::Node routing = root["routing"];
    if (routing) {
        assignIfPresent(routing, "dynamicTimeoutSeconds", config.routing.dynamicTimeoutSeconds);
        assignIfPresent(routing, "rfPriorityHoldoffSeconds", config.routing.rfPriorityHoldoffSeconds);
        assignIfPresent(routing, "disconnectTalkgroup", config.routing.disconnectTalkgroup);
        assignIfPresent(routing, "allowPrivateCalls", config.routing.allowPrivateCalls);
        if (routing["staticTalkgroups"]) {
            for (const auto& tg : routing["staticTalkgroups"]) {
                const uint32_t talkgroup = tg.as<uint32_t>();
                if (config.routing.staticTalkgroups.insert(talkgroup).second) {
                    config.routing.staticTalkgroupOrder.push_back(talkgroup);
                }
            }
        }
        if (routing["talkgroupMappings"]) {
            for (const auto& mapping : routing["talkgroupMappings"]) {
                const uint32_t p25Talkgroup = mapping["p25"].as<uint32_t>();
                const uint32_t bmTalkgroup = mapping["brandmeister"].as<uint32_t>();
                if (p25Talkgroup == 0U || bmTalkgroup == 0U) {
                    throw std::runtime_error("routing.talkgroupMappings entries require non-zero p25 and brandmeister values");
                }
                config.routing.p25ToBmTalkgroups[p25Talkgroup] = bmTalkgroup;
                config.routing.bmToP25Talkgroups[bmTalkgroup] = p25Talkgroup;
            }
        }
    }

    const YAML::Node sms = root["sms"];
    if (sms) {
        assignIfPresent(sms, "enabled", config.sms.enabled);
        assignIfPresent(sms, "bindAddress", config.sms.bindAddress);
        assignIfPresent(sms, "arsPort", config.sms.arsPort);
        assignIfPresent(sms, "tmsPort", config.sms.tmsPort);
        assignIfPresent(sms, "outboundAddress", config.sms.outboundAddress);
        assignIfPresent(sms, "outboundArsPort", config.sms.outboundArsPort);
        assignIfPresent(sms, "outboundTmsPort", config.sms.outboundTmsPort);
        assignIfPresent(sms, "outboundMode", config.sms.outboundMode);
        assignIfPresent(sms, "bmSourceIp", config.sms.bmSourceIp);
        assignIfPresent(sms, "bmTargetIp", config.sms.bmTargetIp);
        assignIfPresent(sms, "bmSlot", config.sms.bmSlot);
        assignIfPresent(sms, "inboxPath", config.sms.inboxPath);
        assignIfPresent(sms, "outboxPath", config.sms.outboxPath);
        assignIfPresent(sms, "sentPath", config.sms.sentPath);
        assignIfPresent(sms, "p25OutboxPath", config.sms.p25OutboxPath);
        assignIfPresent(sms, "serviceRoutePath", config.sms.serviceRoutePath);
        assignIfPresent(sms, "pollIntervalMs", config.sms.pollIntervalMs);
        assignIfPresent(sms, "maxPacketBytes", config.sms.maxPacketBytes);
        assignIfPresent(sms, "decodeUtf16Le", config.sms.decodeUtf16Le);
        assignIfPresent(sms, "outboundAppendNullTerminator", config.sms.outboundAppendNullTerminator);
        if (!sms["serviceRoutePath"]) {
            config.sms.serviceRoutePath = (
                std::filesystem::path(config.sms.outboxPath).parent_path() / "service-routes"
            ).string();
        }
    }

    if (config.sms.outboundMode == "brandmeister") {
        config.sms.bmSlot = config.bm.timeslot;
    }

    return config;
}
