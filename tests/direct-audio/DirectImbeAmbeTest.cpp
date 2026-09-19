#include "DirectImbeAmbe.h"
#include "common/p25/Audio.h"
#include "vocoder/MBEDecoder.h"
#include <array>
#include <vector>
#include <cstdio>
#include <cassert>
#include <cstring>
#include <cmath>
#include <cstdlib>
#include <limits>
#include <stdexcept>
using Word=std::array<uint8_t,11>;
std::vector<std::array<uint8_t,9>> convertAll(const std::vector<Word>& words,FILE*out,unsigned& concealed){
 quantar_direct::reset();vocoder::MBEDecoder decoder(vocoder::DECODE_DMR_AMBE);
 decoder.setGainAdjust(1);decoder.setAutoGain(false);decoder.setUvQuality(12);
 std::vector<std::array<uint8_t,9>> result;
 for(auto& word:words){std::array<uint8_t,9> encoded{};if(!quantar_direct::convert(word.data(),encoded.data()))concealed++;
  float audio[160];assert(decoder.decodeF(encoded.data(),audio)==0);for(float a:audio)assert(std::isfinite(a));
  if(out)fwrite(audio,4,160,out);result.push_back(encoded);
 }return result;
}
int main(int argc,char**argv){
 assert(argc==1 || argc==4);float gain=argc==4 ? std::atof(argv[3]) : 1.58f;quantar_direct::configure(false,gain);assert(!quantar_direct::enabled());
 bool rejected=false;try{quantar_direct::configure(true,std::numeric_limits<float>::quiet_NaN());}catch(const std::invalid_argument&){rejected=true;}assert(rejected);
 quantar_direct::configure(true,gain);assert(quantar_direct::enabled());
 FILE*f=argc==4 ? fopen(argv[1],"rb") : tmpfile();assert(f);
 if(argc==1){p25::Audio fixture;uint8_t frame[216]={};uint8_t word[11]={};for(unsigned i=0;i<9;i++)fixture.encode(frame,word,i);for(unsigned n=0;n<3;n++)fwrite(frame,1,216,f);rewind(f);}p25::Audio audio;uint8_t ldu[216];std::vector<Word> words;unsigned corrected=0;
 while(fread(ldu,1,216,f)==216){corrected+=audio.process(ldu);for(unsigned i=0;i<9;i++){Word w{};audio.decode(ldu,w.data(),i);words.push_back(w);}}
 fclose(f);assert(!words.empty());FILE*out=argc==4 ? fopen(argv[2],"wb") : tmpfile();assert(out);unsigned concealed=0;
 auto first=convertAll(words,out,concealed);fclose(out);unsigned repeated=0;auto second=convertAll(words,nullptr,repeated);assert(first==second);
 // Invalid pitch must yield valid silence without perturbing the next call.
 for(int pitch=208;pitch<=255;pitch++){Word bad{};int v0=(pitch&0xFC)<<4;int v7=(pitch&3)<<1;bad[0]=v0>>4;bad[1]=(v0&15)<<4;bad[10]=v7;
  uint8_t encoded[9];assert(!quantar_direct::convert(bad.data(),encoded));vocoder::MBEDecoder check(vocoder::DECODE_DMR_AMBE);float pcm[160];assert(check.decodeF(encoded,pcm)==0);
 }
 // Sweep all valid pitch codes with varied spectral fields to exercise lookup edges.
 unsigned seed=0x4132;unsigned boundaryWords=0;
 for(int pitch=0;pitch<208;pitch++)for(int k=0;k<4;k++){
  Word w{};for(auto& b:w){seed=seed*1664525u+1013904223u;b=seed>>24;}
  w[0]=(w[0]&3)|(pitch&0xFC);w[10]=(w[10]&~6)|((pitch&3)<<1);
  uint8_t encoded[9];quantar_direct::reset();quantar_direct::convert(w.data(),encoded);
  vocoder::MBEDecoder check(vocoder::DECODE_DMR_AMBE);float pcm[160];assert(check.decodeF(encoded,pcm)==0);for(float a:pcm)assert(std::isfinite(a));boundaryWords++;
 }
 unsigned end=0;auto after=convertAll(words,nullptr,end);assert(first==after);
 printf("PASS frames=%zu gain=%.3f concealed=%u input_corrected_bits=%u boundary_words=%u invalid_pitch_tests=48 reset_deterministic=yes\n",words.size(),gain,concealed,corrected,boundaryWords);
}
