#include "BmPacketDataReassembler.h"

#include "common/dmr/DMRDefines.h"

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace dmr::defines;

namespace {
std::vector<uint8_t> fromHex(const std::string& hex)
{
    if ((hex.size() & 1U) != 0U) {
        throw std::runtime_error("odd hex length");
    }

    std::vector<uint8_t> bytes;
    bytes.reserve(hex.size() / 2U);
    for (size_t i = 0U; i < hex.size(); i += 2U) {
        bytes.push_back(static_cast<uint8_t>(std::stoul(hex.substr(i, 2U), nullptr, 16)));
    }
    return bytes;
}

dmr::data::NetData makeFrame(DataType::E type, const std::string& hex, uint8_t sequence)
{
    const auto bytes = fromHex(hex);
    if (bytes.size() != 33U) {
        throw std::runtime_error("captured DMR payload is not 33 bytes");
    }

    dmr::data::NetData frame;
    frame.setSrcId(262993U);
    frame.setDstId(1000002U);
    frame.setSlotNo(2U);
    frame.setFLCO(FLCO::PRIVATE);
    frame.setSeqNo(sequence);
    frame.setDataType(type);
    frame.setData(bytes.data());
    return frame;
}

std::string toHex(const std::vector<uint8_t>& bytes)
{
    static constexpr char digits[] = "0123456789abcdef";
    std::string hex;
    hex.reserve(bytes.size() * 2U);
    for (uint8_t byte : bytes) {
        hex.push_back(digits[byte >> 4U]);
        hex.push_back(digits[byte & 0x0FU]);
    }
    return hex;
}
}

int main()
{
    const std::vector<std::string> payloads = {
        "2f0510f5211a11e22dc4dfa8058dff57d75df5d33804e4bc1b750ce741480fa5fd",
        "2832882f17fde292ee622d2d463dff57d75df5da84132ce22e2dea682752753b25",
        "26023912cd872277213ce833063dff57d75df5da86bda450207fd22b212d7a1222",
        "2e4b22b6d2ab2d392ed2fed2c63dff57d75df5da8787eff722272f6b22fc8226b2",
        "222112fad22c2d82e3d22822063dff57d75df5da87b22fa222f72f07b2f2e2af72",
        "2fa712e112a92e5279d2b922c63dff57d75df5da86522fb4222d228bb2fcb27712",
        "293cd27ae22a2eb2bb222a22c63dff57d75df5da873b22ab22b7222de2fee27f72",
        "24afd23ae2a621e2a4222f22463dff57d75df5da879922f722f42fe21275e22212",
        "2029d22312a521b2ebd22622063dff57d75df5da873b223422ed2f5712a382a782",
        "20e11226d2ff2792b9222822c63dff57d75df5da87f422e7223b2f1cb22f12f212",
        "216ed22ad226279228d22822463dff57d75df5da87a42fa422f72ff5b273e2af12",
        "25f61234226e283279d23ed2463dff57d75df5da87a62feb2ffb22021225b2fcb2",
        "2837122412e228927322bfd2063dff57d75df5da86662fa72f3d22f5b2ff427422",
        "20222222229b7be22222221c463dff57d75df5da8782222222204db22222222c32",
    };

    BmPacketDataReassembler reassembler;
    auto result = reassembler.push(makeFrame(DataType::DATA_HEADER, payloads.front(), 0U));
    if (!result.consumed || result.packet.has_value()) {
        std::cerr << "header was not accepted\n";
        return 1;
    }

    for (size_t i = 1U; i < payloads.size(); ++i) {
        result = reassembler.push(makeFrame(DataType::RATE_34_DATA, payloads[i], static_cast<uint8_t>(i)));
    }

    if (!result.packet.has_value()) {
        std::cerr << "captured packet was not completed\n";
        return 1;
    }

    const auto& packet = result.packet.value();
    if (packet.sourceRid != 262993U || packet.targetRid != 1000002U ||
        packet.bytes.size() < 28U || (packet.bytes[0U] >> 4U) != 4U || packet.bytes[9U] != 0x11U) {
        std::cerr << "reassembled packet metadata is invalid\n";
        return 1;
    }

    std::cout << "length=" << packet.bytes.size() << " hex=" << toHex(packet.bytes) << '\n';
    return 0;
}
