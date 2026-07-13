#pragma once

#include "AppConfig.h"

namespace bm::protocol {

inline bool isRepeaterDeviceId(uint32_t deviceId)
{
    return deviceId <= 999999U;
}

inline char slotMode(const BMConfig& config)
{
    if (!isRepeaterDeviceId(config.repeaterId)) {
        return '4';
    }
    if (config.slot1 && config.slot2) {
        return '3';
    }
    if (config.slot1) {
        return '1';
    }
    if (config.slot2) {
        return '2';
    }
    return '0';
}

}
