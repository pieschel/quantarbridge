#include "BMProtocol.h"

#include <iostream>

int main()
{
    BMConfig config;
    config.slot1 = false;
    config.slot2 = true;

    config.repeaterId = 123456789U;
    if (bm::protocol::slotMode(config) != '4') {
        std::cerr << "hotspot must report simplex slot mode 4\n";
        return 1;
    }

    config.repeaterId = 123456U;
    if (bm::protocol::slotMode(config) != '2') {
        std::cerr << "TS2-only repeater must report slot mode 2\n";
        return 1;
    }

    config.slot1 = true;
    config.slot2 = true;
    if (bm::protocol::slotMode(config) != '3') {
        std::cerr << "dual-slot repeater must report slot mode 3\n";
        return 1;
    }

    return 0;
}
