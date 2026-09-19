#include "DirectAmbeImbe.h"
#include "vocoder/MBEDecoder.h"
#include "vocoder/MBEEncoder.h"
#include "common/p25/Audio.h"
#include <array>
#include <vector>
#include <cstdio>
#include <cassert>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
using Input=std::array<uint8_t,9>;using Output=std::array<uint8_t,11>;
void verifyWord(const Output&word){
 p25::Audio audio;uint8_t ldu[216]={};for(unsigned n=0;n<9;n++)audio.encode(ldu,word.data(),n);
 assert(audio.process(ldu)==0);for(unsigned n=0;n<9;n++){uint8_t decoded[11];audio.decode(ldu,decoded,n);assert(std::memcmp(word.data(),decoded,11)==0);}
}
std::vector<Output> run(const std::vector<Input>&input,FILE*out,unsigned&concealed){
 quantar_reverse::reset();vocoder::MBEDecoder decoder(vocoder::DECODE_88BIT_IMBE);
 decoder.setAutoGain(false);decoder.setGainAdjust(1);decoder.setUvQuality(12);std::vector<Output> result;
 for(auto&w:input){Output coded{};if(!quantar_reverse::convert(w.data(),coded.data()))concealed++;verifyWord(coded);
  float samples[160];decoder.decodeF(coded.data(),samples);for(float s:samples)assert(std::isfinite(s));if(out)fwrite(samples,4,160,out);result.push_back(coded);
 }return result;
}
int main(int argc,char**argv){
 assert(argc==1 || argc==3);quantar_reverse::configure(false,4,1);assert(!quantar_reverse::enabled());bool bad=false;
 try{quantar_reverse::configure(true,std::numeric_limits<float>::quiet_NaN(),1);}catch(const std::invalid_argument&){bad=true;}assert(bad);
 quantar_reverse::configure(true,4,1);assert(quantar_reverse::enabled());
 FILE*in=argc==3 ? fopen(argv[1],"rb") : tmpfile();assert(in);
 if(argc==1){const uint8_t quiet[9]={0xB9,0xE8,0x81,0x52,0x61,0x73,0x00,0x2A,0x6B};for(unsigned n=0;n<27;n++)fwrite(quiet,1,9,in);rewind(in);}std::vector<Input> inputs;Input word;while(fread(word.data(),1,9,in)==9)inputs.push_back(word);fclose(in);assert(!inputs.empty());
 FILE*out=argc==3 ? fopen(argv[2],"wb") : tmpfile();assert(out);unsigned concealed=0,secondCount=0;auto first=run(inputs,out,concealed);fclose(out);auto second=run(inputs,nullptr,secondCount);assert(first==second);
 vocoder::MBEEncoder fec(vocoder::ENCODE_DMR_AMBE);unsigned special=0;
 for(unsigned pitch=0;pitch<128;pitch++){
  uint8_t bits[72]={};int pos[7]={0,1,2,3,37,38,39};for(int i=0;i<7;i++)bits[pos[i]]=(pitch>>(6-i))&1;
  Input frame{};fec.encodeBits(bits,frame.data());Output result{};bool valid=quantar_reverse::convert(frame.data(),result.data());
  if((pitch>=120&&pitch<=123)||pitch>=126){assert(!valid);special++;}verifyWord(result);
 }
 unsigned seed=0x5025;for(unsigned i=0;i<1500;i++){Input frame{};for(auto&b:frame){seed=seed*1664525u+1013904223u;b=seed>>24;}Output result{};quantar_reverse::convert(frame.data(),result.data());verifyWord(result);}
 // A prolonged erasure burst must end in stable valid silence, then reset cleanly.
 uint8_t bits[72]={};bits[0]=bits[1]=bits[2]=bits[3]=1;Input erased{};fec.encodeBits(bits,erased.data());
 for(int i=0;i<30;i++){Output result{};assert(!quantar_reverse::convert(erased.data(),result.data()));verifyWord(result);}
 unsigned recovered=0;auto third=run(inputs,nullptr,recovered);assert(first==third);
 printf("PASS frames=%zu concealed=%u reset_deterministic=yes pitch_cases=128 special_cases=%u corrupted_words=1500 long_erasure=30 P25_FEC=valid\n",inputs.size(),concealed,special);
}
