#!/usr/bin/env python3
"""Execute the actual idle scheduler with an in-memory clock/transport.

The production method body is extracted unchanged. Only its clock, codec and
transport dependencies are simulated; no device, network or recorded audio is
used. Converter predictor ownership is covered by DirectImbeAmbeTest.cpp.
"""

import argparse
import os
from pathlib import Path
import subprocess


PRELUDE = r"""
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <mutex>
#include <vector>
constexpr unsigned TX_MODE_DMR=0, TX_MODE_P25=1, NET_STAT_RUNNING=1;
namespace P25DEF { constexpr unsigned ALGO_UNENCRYPT=0x80; }
namespace dmr { namespace lc {
struct LC { unsigned src=123456, dst=91; unsigned getSrcId()const{return src;}
    unsigned getDstId()const{return dst;} };
}}
namespace vocoder {
struct P25ToAmbeFloat {
    struct Frame { std::array<uint8_t,9> bytes{}; bool valid=true;
        bool hasFrame()const{return valid;} };
    bool active=true, fail=false; unsigned idleCalls=0;
    bool enabled()const{return active;}
    void idleFrame(Frame& f){ ++idleCalls; f.valid=!fail; f.bytes.fill(0x5a); }
};
}
struct Timer { bool running=true, expired=false; unsigned starts=0;
    bool isRunning()const{return running;} bool hasExpired()const{return expired;} };
struct Network { bool connected=true, queued=false;
    unsigned getStatus()const{return connected?NET_STAT_RUNNING:0;}
    bool hasP25Data()const{return queued;} };
struct Embedded { bool present=true; unsigned src=123456, dst=91;
    std::unique_ptr<dmr::lc::LC> getLC(){
        if(!present)return nullptr;
        auto p=std::make_unique<dmr::lc::LC>();p->src=src;p->dst=dst;return p;
    }
};
struct HostBridge {
    static std::mutex s_networkMutex;
    static uint64_t now;
    static uint64_t directAudioNowMs(){return now;}
    std::recursive_mutex m_directCodecMutex;
    bool m_directP25ToAmbeEnabled=true, m_callInProgress=true, m_ignoreCall=false;
    unsigned m_txMode=TX_MODE_DMR, m_callAlgoId=P25DEF::ALGO_UNENCRYPT;
    unsigned m_txStreamId=1, m_ambeCount=0, sent=0;
    uint64_t m_directForwardNextIdleMs=100;
    vocoder::P25ToAmbeFloat codec;
    vocoder::P25ToAmbeFloat* m_p25ToAmbeFloat=&codec;
    Network network;
    Network* m_network=&network;
    Timer m_networkWatchdog;
    Embedded m_dmrEmbeddedData;
    void encodeDMRAudioFrame(uint8_t* pcm,unsigned src,unsigned dst,const uint8_t* word){
        assert(pcm==nullptr && src==123456 && dst==91 && word[0]==0x5a);++sent;
    }
    void serviceDirectP25ToDmrIdle();
};
std::mutex HostBridge::s_networkMutex;
uint64_t HostBridge::now=0;
"""

MAIN = r"""
int main(){
    // The first deadline is inclusive, subsequent bursts have 60ms spacing.
    HostBridge h;HostBridge::now=99;h.serviceDirectP25ToDmrIdle();assert(h.sent==0);
    HostBridge::now=100;h.serviceDirectP25ToDmrIdle();assert(h.sent==3);
    assert(h.m_directForwardNextIdleMs==160 && h.m_networkWatchdog.starts==0);
    HostBridge::now=159;h.serviceDirectP25ToDmrIdle();assert(h.sent==3);
    HostBridge::now=160;h.serviceDirectP25ToDmrIdle();assert(h.sent==6);
    // A stalled task emits one burst, never a catch-up train.
    HostBridge::now=10000;h.serviceDirectP25ToDmrIdle();assert(h.sent==9);
    h.serviceDirectP25ToDmrIdle();assert(h.sent==9);
    assert(h.m_directForwardNextIdleMs==10060);
    // Every call, privacy, pending-source and transport guard suppresses idle.
    for(unsigned mode=0;mode<18;++mode){
        HostBridge b;HostBridge::now=100;
        switch(mode){
        case 0:b.m_directP25ToAmbeEnabled=false;break;
        case 1:b.m_txMode=TX_MODE_P25;break;
        case 2:b.m_p25ToAmbeFloat=nullptr;break;
        case 3:b.codec.active=false;break;
        case 4:b.m_network=nullptr;break;
        case 5:b.m_callInProgress=false;break;
        case 6:b.m_ignoreCall=true;break;
        case 7:b.m_callAlgoId=0x84;break;
        case 8:b.m_networkWatchdog.running=false;break;
        case 9:b.m_networkWatchdog.expired=true;break;
        case 10:b.m_txStreamId=0;break;
        case 11:b.m_ambeCount=1;break;
        case 12:b.m_directForwardNextIdleMs=0;break;
        case 13:b.network.connected=false;break;
        case 14:b.network.queued=true;break;
        case 15:b.m_dmrEmbeddedData.present=false;break;
        case 16:b.m_dmrEmbeddedData.src=0;break;
        case 17:b.m_dmrEmbeddedData.dst=0;break;
        }
        b.serviceDirectP25ToDmrIdle();assert(b.sent==0 && b.codec.idleCalls==0);
    }
    // A newly queued source packet has priority over an already due idle tick.
    HostBridge q;HostBridge::now=100;q.network.queued=true;
    q.serviceDirectP25ToDmrIdle();assert(q.sent==0);
    q.network.queued=false;q.serviceDirectP25ToDmrIdle();assert(q.sent==3);
    // Codec failure disarms scheduling instead of looping or sending bad data.
    HostBridge f;f.codec.fail=true;f.serviceDirectP25ToDmrIdle();
    assert(f.sent==0 && f.codec.idleCalls==1 && f.m_directForwardNextIdleMs==0);
    std::puts("PASS actual forward idle scheduler: 60ms cadence, delayed tick, 18 guards, source priority, failure disarm");
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--compiler", nargs="+", default=["c++"])
    args = parser.parse_args()
    text = (args.source / "src/bridge/HostBridge.cpp").read_text(encoding="utf-8")
    begin = text.index("void HostBridge::serviceDirectP25ToDmrIdle()")
    brace = text.index("{", begin)
    depth, end = 1, brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    args.work_dir.mkdir(parents=True, exist_ok=True)
    source = args.work_dir / "forward-cadence.cpp"
    binary = args.work_dir / ("forward-cadence-test.exe" if os.name == "nt" else "forward-cadence-test")
    source.write_text(PRELUDE + text[begin:end] + MAIN, encoding="utf-8")
    subprocess.run([*args.compiler, "-std=c++17", "-O1", "-pthread", str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
