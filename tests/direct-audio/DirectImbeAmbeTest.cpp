// SPDX-License-Identifier: GPL-2.0-only
#include "vocoder/P25ToAmbeFloat.h"
#include "vocoder/MBEDecoder.h"
#include "common/p25/P25Defines.h"
#include <array>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <limits>
#include <vector>

using Codec = vocoder::P25ToAmbeFloat;
using Word = std::array<uint8_t, 11>;
using Output = std::array<uint8_t, 9>;

Word syntheticWord(unsigned pitch, unsigned seed)
{
    Word word{};
    for (auto& byte : word) {
        seed = seed * 1664525U + 1013904223U;
        byte = static_cast<uint8_t>(seed >> 24);
    }
    word[0] = static_cast<uint8_t>((word[0] & 3U) | (pitch & 0xFCU));
    word[10] = static_cast<uint8_t>((word[10] & ~6U) | ((pitch & 3U) << 1));
    return word;
}

void verifyAmbe(const Output& word)
{
    vocoder::MBEDecoder decoder(vocoder::DECODE_DMR_AMBE);
    decoder.setAutoGain(false);
    decoder.setGainAdjust(1);
    float samples[160];
    assert(decoder.decodeF(word.data(), samples) == 0);
    for (float sample : samples) assert(std::isfinite(sample));
}

std::vector<Output> run(Codec& codec, const std::vector<Word>& input)
{
    codec.resetCall();
    std::vector<Output> result;
    for (const auto& word : input) {
        Codec::Frame output;
        assert(codec.convert(word.data(), word.size(), false, output) == Codec::Status::Voice);
        assert(output.hasFrame());
        verifyAmbe(output.bytes);
        result.push_back(output.bytes);
    }
    return result;
}

int main()
{
    Codec::Config config;
    config.gain = 1.5f;
    config.scale = 0.9f;
    config.cutoffHz = 0;
    Codec codec(config);
    Codec::Frame output;
    auto word = syntheticWord(80, 123);
    assert(codec.convert(word.data(), word.size(), false, output) == Codec::Status::Disabled);
    assert(!output.hasFrame());
    codec.setEnabled(true);
    assert(codec.convert(nullptr, 11, false, output) == Codec::Status::RejectedSize);
    assert(codec.convert(word.data(), 10, false, output) == Codec::Status::RejectedSize);

    std::vector<Word> input;
    for (unsigned pitch = 0; pitch < 208; ++pitch)
        for (unsigned variant = 0; variant < 4; ++variant)
            input.push_back(syntheticWord(pitch, 0x4312U + 17U * pitch + variant));
    const auto first = run(codec, input);
    assert(first == run(codec, input));

    // A rejected frame must leave both model histories unchanged.
    codec.resetCall();
    Codec reference(config);
    reference.setEnabled(true);
    for (unsigned i = 0; i < 8; ++i) {
        Codec::Frame a, b;
        assert(codec.convert(input[i].data(), 11, false, a) == Codec::Status::Voice);
        auto invalid = syntheticWord(255, 9);
        assert(codec.convert(invalid.data(), 11, false, output) == Codec::Status::RejectedPitch);
        assert(reference.convert(input[i].data(), 11, false, b) == Codec::Status::Voice);
        assert(a.bytes == b.bytes);
    }
    for (unsigned pitch = 208; pitch < 256; ++pitch) {
        const auto invalid = syntheticWord(pitch, 21);
        const auto status = codec.convert(invalid.data(), 11, false, output);
        if (pitch >= 216 && pitch <= 219) {
            assert(status == Codec::Status::Silence);
            verifyAmbe(output.bytes);
        } else {
            assert(status == Codec::Status::RejectedPitch);
            assert(!output.hasFrame());
        }
    }
    codec.resetCall();
    assert(codec.convert(word.data(), 11, false, output) == Codec::Status::Voice);
    for (unsigned i = 0; i < 8; ++i) {
        assert(codec.concealErasure(output) == (i < 3 ? Codec::Status::Concealed : Codec::Status::Muted));
        verifyAmbe(output.bytes);
    }
    assert(codec.silenceFrame(output) == Codec::Status::Silence);
    verifyAmbe(output.bytes);
    assert(codec.convert(word.data(), 11, true, output) == Codec::Status::RejectedEncrypted);
    assert(!output.hasFrame());
    assert(first == run(codec, input));

    for (unsigned kind = 0; kind < 4; ++kind) {
        Codec::Config bad = config;
        if (kind == 0) bad.gain = std::numeric_limits<float>::quiet_NaN();
        if (kind == 1) bad.scale = 0;
        if (kind == 2) bad.cutoffHz = 4000;
        if (kind == 3) bad.deessMaxDb = 3;
        Codec rejected(bad);
        rejected.setEnabled(true);
        assert(rejected.convert(word.data(), 11, false, output) == Codec::Status::RejectedConfig);
        assert(!output.hasFrame());
    }
    std::printf("PASS active P25ToAmbeFloat: 832 pitch/spectrum words, reset, rejection transaction, silence, concealment, encrypted guard, AMBE FEC\n");
}
