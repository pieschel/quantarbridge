// SPDX-License-Identifier: GPL-2.0-only
#include "vocoder/AmbeToP25Float.h"
#include "vocoder/MBEEncoder.h"
#include "vocoder/MBEDecoder.h"
#include "common/p25/Audio.h"
#include "common/p25/P25Defines.h"
#include "P25ImbeSync.h"
#include <array>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <vector>

using Codec = vocoder::AmbeToP25Float;
using Input = std::array<uint8_t, 9>;
using Output = std::array<uint8_t, 11>;

template<std::size_t N>
void field(uint8_t* bits, const int (&positions)[N], unsigned value)
{
    for (unsigned i = 0; i < N; ++i) bits[positions[i]] = (value >> (N - i - 1)) & 1U;
}

// Synthetic legal parameter words. No recorded speech or firmware is required.
Input makeAmbe(unsigned pitch, unsigned voicing = 0, unsigned gain = 16)
{
    uint8_t bits[49] = {};
    const int pitchBits[] = {0, 1, 2, 3, 37, 38, 39};
    const int voicingBits[] = {4, 5, 6, 7, 35};
    const int gainBits[] = {8, 9, 10, 11, 36};
    field(bits, pitchBits, pitch);
    field(bits, voicingBits, voicing);
    field(bits, gainBits, gain);
    Input encoded{};
    vocoder::MBEEncoder fec(vocoder::ENCODE_DMR_AMBE);
    fec.encodeBits(bits, encoded.data());
    return encoded;
}

void verifyWord(const Output& word)
{
    p25::Audio fec;
    uint8_t ldu[216] = {};
    for (unsigned i = 0; i < 9; ++i) fec.encode(ldu, word.data(), i);
    assert(fec.process(ldu) == 0);
    for (unsigned i = 0; i < 9; ++i) {
        Output extracted{};
        fec.decode(ldu, extracted.data(), i);
        assert(extracted == word);
    }
    vocoder::MBEDecoder decoder(vocoder::DECODE_88BIT_IMBE);
    decoder.setAutoGain(false);
    decoder.setGainAdjust(1);
    float pcm[160];
    auto input = word;
    decoder.decodeF(input.data(), pcm);
    for (float sample : pcm) assert(std::isfinite(sample));
}

std::vector<Output> run(Codec& codec, const std::vector<Input>& inputs)
{
    codec.reset();
    std::vector<Output> result;
    for (const auto& input : inputs) {
        Output output{};
        codec.convert(input.data(), output.data());
        verifyWord(output);
        result.push_back(output);
    }
    return result;
}

void checkSyncBits()
{
    const unsigned offsets[9] = {10, 26, 55, 80, 105, 130, 155, 180, 204};
    for (bool ldu1 : {false, true}) {
        std::array<uint8_t, 225> original;
        original.fill(0xA6);
        for (bool direct : {false, true}) for (bool clear : {false, true}) {
            auto actual = original, expected = original;
            if (direct && clear) for (unsigned i = 0; i < 9; ++i)
                expected[offsets[i] + 10] = (expected[offsets[i] + 10] & 0xFEU) |
                    (((ldu1 ? 0U : 1U) + i) & 1U);
            bridge::stampP25ImbeSync(actual.data(), ldu1, direct, clear);
            assert(actual == expected);
        }
    }
    bridge::stampP25ImbeSync(nullptr, true, true, true);
}

int main()
{
    Codec codec;
    assert(codec.setLevel(4, -0.15956886662028558f));
    assert(codec.setDeessDb(0));
    assert(!codec.setLevel(0, 0));
    assert(!codec.setLevel(4, std::numeric_limits<float>::quiet_NaN()));
    assert(!codec.setDeessDb(3));
    std::vector<Input> inputs;
    for (unsigned pitch = 0; pitch < 128; ++pitch) {
        for (unsigned v : {0U, 16U, 18U, 31U}) inputs.push_back(makeAmbe(pitch, v));
    }
    const auto first = run(codec, inputs);
    assert(first == run(codec, inputs));

    Output output{};
    for (unsigned pitch = 0; pitch < 128; ++pitch) {
        codec.reset();
        const auto input = makeAmbe(pitch);
        const auto result = codec.convert(input.data(), output.data());
        assert(result.fecCorrections == 0);
        if (pitch >= 120 && pitch <= 123) {
            assert(!result.sourceAccepted);
            assert(result.status == Codec::Status::Muted);
        } else if (pitch >= 126) {
            assert(!result.sourceAccepted);
            assert(result.status == Codec::Status::ToneMuted);
        } else {
            assert(result.sourceAccepted);
            assert(result.status == (pitch >= 124 ? Codec::Status::CodedSilence : Codec::Status::Voice));
        }
        verifyWord(output);
    }

    // Correctable input damage must produce the same word and freeze learning.
    auto voice = makeAmbe(50);
    codec.reset();
    Output pristine{};
    codec.convert(voice.data(), pristine.data());
    auto damaged = voice;
    damaged[0] ^= 0x80;
    codec.reset();
    const auto corrected = codec.convert(damaged.data(), output.data());
    assert(corrected.fecCorrections == 1 && corrected.sourceAccepted);
    assert(pristine == output);

    codec.reset();
    assert(codec.convert(voice.data(), output.data()).sourceAccepted);
    for (unsigned i = 0; i < 8; ++i) {
        const auto missing = codec.convert(nullptr, output.data());
        assert(missing.status == (i < 3 ? Codec::Status::Concealed : Codec::Status::Muted));
        verifyWord(output);
    }
    assert(codec.convert(voice.data(), output.data(), true).status == Codec::Status::EncryptedMuted);
    assert(std::memcmp(output.data(), p25::defines::NULL_IMBE, output.size()) == 0);
    assert(first == run(codec, inputs));

    // The public enabled API must learn ordinary voiced frames, keep a bounded
    // correction, and never learn station loudness from UV, extra-grid or CN.
    Codec::LevelerConfig config;
    config.enabled = true;
    assert(codec.setLevelerConfig(config));
    auto bad = config;
    bad.targetDbfs = -19;
    assert(!codec.setLevelerConfig(bad));
    bad = config; bad.maxBoostDb = 13; assert(!codec.setLevelerConfig(bad));
    bad = config; bad.maxCutDb = 19; assert(!codec.setLevelerConfig(bad));

    unsigned trainedGain = 32;
    for (unsigned gain = 0; gain < 32; ++gain) {
        codec.reset();
        voice = makeAmbe(50, 0, gain);
        for (unsigned i = 0; i < 40; ++i) codec.convert(voice.data(), output.data());
        if (codec.getLevelerDiagnostics().estimateReady) { trainedGain = gain; break; }
    }
    assert(trainedGain < 32);
    voice = makeAmbe(50, 0, trainedGain);
    for (unsigned i = 0; i < 160; ++i) {
        assert(codec.convert(voice.data(), output.data()).sourceAccepted);
        const auto d = codec.getLevelerDiagnostics();
        assert(d.enabled && d.estimateReady);
        assert(d.levelGainDb >= -18.00001 && d.levelGainDb <= 12.00001);
        assert(d.appliedGainDb >= -42.00001 && d.appliedGainDb <= 12.00001);
        assert(std::isfinite(d.predictedPeakBound) && d.predictedPeakBound >= 0);
        assert(std::isfinite(d.peakCapDb));
    }
    assert(codec.getLevelerDiagnostics().learnedWindows > 0);
    for (unsigned v : {16U, 18U, 31U}) {
        const auto noise = makeAmbe(50, v, trainedGain);
        for (unsigned i = 0; i < 36; ++i) {
            const auto before = codec.getLevelerDiagnostics();
            codec.convert(noise.data(), output.data());
            const auto after = codec.getLevelerDiagnostics();
            assert(after.learnedWindows == before.learnedWindows);
            assert(after.appliedGainDb <= before.appliedGainDb + 1.0e-5);
            verifyWord(output);
        }
    }
    for (unsigned pitch : {124U, 125U}) {
        const auto silence = makeAmbe(pitch);
        codec.convert(silence.data(), output.data());
        const auto d = codec.getLevelerDiagnostics();
        assert(!d.learnedThisFrame && d.appliedGainDb <= 0.00001);
    }
    const auto generation = codec.getLevelerDiagnostics().resetGeneration;
    codec.reset();
    const auto reset = codec.getLevelerDiagnostics();
    assert(reset.resetGeneration > generation);
    assert(!reset.estimateReady && reset.confirmedVoiceFrames == 0 && reset.appliedGainDb == 0);
    assert(run(codec, inputs) == run(codec, inputs));
    checkSyncBits();
    std::printf("PASS active AmbeToP25Float: pitch/SourceGrid sweep, FEC repair, reset, concealment, encryption, leveler learning/noise bounds, P25 sync/FEC\n");
}
