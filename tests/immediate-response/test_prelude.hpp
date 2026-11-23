#include <cstdint>
#include <cstring>
#include <memory>
#include <vector>
#include <string>
#include <unordered_map>
#include <mutex>
#include <cassert>
#include <cstdio>
#include <array>
#include <deque>
#include <algorithm>
#include <fstream>
#define P25_PDU_STR "PDU"
#define __IP_FROM_UINT(v) std::string("test")
#define __IP_FROM_STR(v) 0x0A000001U
#define DECLARE_UINT8_ARRAY(n,l) std::vector<uint8_t> n##_storage(l,0);uint8_t* n=n##_storage.data()
using ulong64_t=uint64_t;
using UInt8Array=std::unique_ptr<uint8_t[]>;
static constexpr unsigned MFG_STANDARD=0,LOG_RF=0,LOG_P25=1,RS_RF_LISTENING=0,RS_RF_DATA=1,RS_NET_IDLE=0,RS_NET_AUDIO=1,RS_NET_DATA=2;
static constexpr unsigned P25_PDU_FRAME_LENGTH_BYTES=512,P25_PDU_HEADER_LENGTH_BYTES=12,WUID_FNE=1,P25_TDU_FRAME_LENGTH_BYTES=18,P25_TDU_FRAME_LENGTH_BITS=144,MAX_PREAMBLE_TDU_CNT=64;
static constexpr unsigned P25_MAX_PDU_BLOCKS=42,P25_PDU_UNCONFIRMED_LENGTH_BYTES=12,P25_PDU_CONFIRMED_DATA_LENGTH_BYTES=16,P25_PDU_FEC_LENGTH_BITS=196,P25_PDU_FEC_LENGTH_BYTES=25,P25_PREAMBLE_LENGTH_BITS=112;
static constexpr uint32_t g_motorolaArsServerIp=0x0A000002,kMotorolaTmsServiceFallbackDelayMs=2800;
namespace modem{enum{TAG_DATA=1,TAG_EOT=2};}namespace PDUFormatType{enum{CONFIRMED=1,RSP=2};}namespace PDUSAP{enum{PACKET_DATA=4,CONV_DATA_REG=32};}namespace DUID{enum{PDU=12,TDU=3};}namespace PDURegType{enum{CONNECT=0,DISCONNECT=1,ACCEPT=4,DENY=5};}namespace PDUAckClass{enum{ACK=0,NACK=1,ACK_RETRY=2};}namespace PDUAckType{enum{ACK=1,NACK_UNDELIVERABLE=2,NACK_ILLEGAL=3,NACK_PACKET_CRC=4,NACK_SEQ=5,NACK_OUT_OF_SEQ=6};}
template<class...T>void LogInfoEx(T...){}template<class...T>void LogWarning(T...){}template<class...T>void LogError(T...){}
template<class...T>void writeRfPacketDataArtifact(T...){}template<class...T>void writeP25AssembledArtifact(T...){}template<class...T>void writeP25RawFrameArtifact(T...){}
namespace lookups{struct RadioId{bool radioDefault()const{return true;}bool radioEnabled()const{return true;}std::string radioIPAddress()const{return "";}};}
struct Lookup{lookups::RadioId find(uint32_t){return{};}};
namespace acl{struct AccessControl{static bool valid;static bool validateSrcId(uint32_t){return valid;}};bool AccessControl::valid=true;}
namespace data{struct DataHeader{
std::array<uint8_t,12>b{{}};uint32_t len=12;uint32_t src=0;
void setFormat(unsigned v){b[0]=v;}unsigned getFormat()const{return b[0];}
void setMFId(unsigned v){b[1]=v;}unsigned getMFId()const{return b[1];}
void setAckNeeded(bool v){b[2]=(b[2]&~1)|v;}bool getAckNeeded()const{return b[2]&1;}
void setOutbound(bool v){b[2]=(b[2]&~2)|(v?2:0);}void setSAP(unsigned v){b[3]=v;}unsigned getSAP()const{return b[3];}
void setLLId(uint32_t v){b[4]=v>>16;b[5]=v>>8;b[6]=v;}uint32_t getLLId()const{return(b[4]<<16)|(b[5]<<8)|b[6];}
void setSynchronize(bool v){b[2]=(b[2]&~4)|(v?4:0);}void setLastFragment(bool v){b[2]=(b[2]&~8)|(v?8:0);}
void setNs(unsigned v){b[7]=v;}unsigned getNs()const{return b[7];}void setFSN(unsigned v){b[8]=v;}
void setBlocksToFollow(unsigned v){b[9]=v;}unsigned getBlocksToFollow()const{return b[9];}
void setResponseClass(unsigned v){b[7]=v;}unsigned getResponseClass()const{return b[7];}void setResponseType(unsigned v){b[8]=v;}unsigned getResponseType()const{return b[8];}void setResponseStatus(unsigned v){b[10]=v;}unsigned getResponseStatus()const{return b[10];}
void setSrcLLId(uint32_t v){src=v;}uint32_t getSrcLLId()const{return src;}void setFullMessage(bool v){b[11]=v;}
void calculateLength(uint32_t v){len=v;b[9]=(v+15)/16;}uint32_t getPacketLength()const{return len;}unsigned getPadLength()const{return 0;}
void getData(uint8_t*p)const{memcpy(p,b.data(),12);}void encode(uint8_t*p)const{memcpy(p,b.data(),12);memcpy(p+12,b.data(),12);}
};}
using DataHeader=data::DataHeader;
struct Block{unsigned serial=0;unsigned getFormat(){return PDUFormatType::CONFIRMED;}unsigned getSerialNo(){return serial;}unsigned getData(uint8_t*p){for(unsigned i=0;i<16;i++)p[i]=serial*16+i;return 16;}};
struct Assembler{data::DataHeader dataHeader;Block dataBlocks[16];bool ext=false,crc=false;unsigned bad=0;Assembler(){for(unsigned i=0;i<16;i++)dataBlocks[i].serial=i;}bool getExtendedAddress(){return ext;}bool getPacketCRCFailed(){return crc;}unsigned getUndecodableBlockCount(){return bad;}
UInt8Array assemble(data::DataHeader&h,bool ex,bool aux,const uint8_t*p,uint32_t*bits){const unsigned n=12+h.len+2;*bits=n*8;UInt8Array out(new uint8_t[n]{});h.getData(out.get());memcpy(out.get()+12,p,h.len);out[n-2]=ex;out[n-1]=aux;return out;}};
static bool parseMotorolaScepArsRegistration(const std::vector<uint8_t>&,uint32_t,uint32_t&ip,uint16_t&id){ip=0x0a000001;id=7;return true;}
static bool parseMotorolaArsRefresh(const uint8_t*p,unsigned n,uint32_t,uint32_t&ip,uint16_t&id,uint8_t&opcode){if(!n||p[0]==0xff)return false;ip=0x0a000001;id=p[0];opcode=p[1];return true;}
static std::vector<uint8_t>buildMotorolaArsSuccess(uint32_t ip,uint16_t id){std::vector<uint8_t>p(32,0x42);memcpy(p.data(),&ip,4);memcpy(p.data()+4,&id,2);return p;}
static std::vector<uint8_t>buildMotorolaTmsServiceAvailability(uint32_t ip){return buildMotorolaArsSuccess(ip,0x33);}
static uint32_t motorolaRfResponderRid(uint32_t rid){return rid;}
template<class...T>void rememberMotorolaPacketNs(T...){}template<class...T>void writeMotorolaSessionTrace(T...){}
namespace lc{namespace tsbk{struct IOSP_MSG_UPDT{void setSrcId(uint32_t){}void setDstId(uint32_t){}void setMessage(uint8_t){}};}}
struct Utils{template<class...T>static void dump(T...){}static void setBitRange(const uint8_t*p,uint8_t*out,unsigned offset,unsigned n){for(unsigned i=0;i<n;i++){unsigned bit=(p[i/8]>>(7-i%8))&1;unsigned pos=offset+i;out[pos/8]=(out[pos/8]&~(1<<(7-pos%8)))|(bit<<(7-pos%8));}}};
struct P25Utils{static unsigned encodeByLength(const uint8_t*p,uint8_t*out,unsigned n){memcpy(out,p,(n+7)/8);return n;}static void addStatusBits(uint8_t*p,unsigned,bool inbound,bool){p[9]=(p[9]&~1)|(inbound?1:0);}static void setStatusBitsStartIdle(uint8_t*p){p[10]=0x5a;}static void setStatusBitsAllIdle(uint8_t*p,unsigned){p[9]=0x55;}};
struct Sync{static void addP25Sync(uint8_t*p){for(unsigned i=0;i<6;i++)p[i]=0x70+i;}};
struct Queue{std::vector<uint8_t>bytes;size_t cap=32;unsigned resizes=0;bool isEmpty()const{return bytes.empty();}size_t freeSpace()const{return cap-bytes.size();}void resize(size_t n){assert(bytes.empty());cap=n;resizes++;}bool addData(const uint8_t*p,size_t n){if(n>freeSpace())return false;bytes.insert(bytes.end(),p,p+n);return true;}void clear(){bytes.clear();}};
struct Control{static std::mutex s_queueLock;unsigned m_netState=RS_NET_IDLE,m_rfState=RS_RF_LISTENING;Queue m_txQueue,m_txImmQueue;bool m_duplex=true,m_isModemDFSI=false,m_ccRunning=false;unsigned m_tduPreambleCount=3;Lookup*m_ridLookup=nullptr;
struct Signaling{unsigned writes=0;template<class...T>void writeRF_TSDU_SBF_Imm(T...){writes++;}}signaling;Signaling*m_control=&signaling;
struct Modem{bool tx=false;bool hasTX(){return tx;}}modem;Modem*m_modem=&modem;
struct Voice{void writeNetwork(const uint8_t*,unsigned){}}voice;Voice*m_voice=&voice;
struct Timer{bool run=false,expired=false;bool isRunning(){return run;}bool hasExpired(){return expired;}}m_rfTimeout;
struct NID{void encode(uint8_t*p,unsigned v){p[7]=v;}}m_nid;
void writeRF_TDU(bool,bool=false);void writeRF_Nulls();void writeRF_Preamble(uint32_t=0,bool=false);
void addFrame(const uint8_t*p,uint32_t n,bool=false,bool imm=false){auto&q=imm?m_txImmQueue:m_txQueue;if(q.freeSpace()<n+2)q.cap=q.bytes.size()+n+1024;uint8_t len[2]={uint8_t(n>>8),uint8_t(n)};const bool a=q.addData(len,2),b=q.addData(p,n);assert(a&&b);(void)a;(void)b;}
size_t effects()const{return m_txQueue.bytes.size()+m_txImmQueue.bytes.size()+signaling.writes;}void clear(){m_txQueue.clear();m_txImmQueue.clear();signaling.writes=0;}};
std::mutex Control::s_queueLock;
class Data{public:
#include STRUCTS_HEADER
#ifndef BASELINE
#include MEMBERS_HEADER
bool deferMotorolaDataForNetworkVoice();
void handleDecodedResponse();
void clockImmediatePrefixForTest(uint32_t);unsigned timedWrites=0;
#endif
Control ctl;Control*m_p25=&ctl;Assembler assembler;Assembler*m_rfAssembler=&assembler;bool m_inbound=false,m_verbose=false,m_dumpPDUData=false,m_motorolaNetworkVoiceDrainPending=false;
std::unordered_map<uint32_t,MotorolaSession>m_motorolaSessions;std::unordered_map<uint32_t,uint32_t>m_fneRegTable;uint8_t*m_retryPDUData=nullptr;uint32_t m_retryPDUBitLength=0;uint8_t m_retryCount=0;std::vector<uint8_t>payload=std::vector<uint8_t>(53,0x22);uint8_t*m_rfPduUserData=payload.data();uint32_t m_rfPduUserDataLength=53;unsigned remembered=0,aborted=0;
Data(){assembler.dataHeader.setMFId(0x90);assembler.dataHeader.setFormat(PDUFormatType::CONFIRMED);assembler.dataHeader.setAckNeeded(true);assembler.dataHeader.setNs(3);assembler.dataHeader.setLLId(123);assembler.dataHeader.setBlocksToFollow(4);payload[1]=0x10;}
~Data(){delete[]m_retryPDUData;}void rememberLocalSmsRid(uint32_t){remembered++;}void resetMotorolaTmsInboundDelivery(MotorolaSession&s){aborted++;s.pendingTmsFragmentAck=false;}
void restartMotorolaTmsInboundDelivery(MotorolaSession&s,uint32_t){aborted++;s.pendingTmsFragmentIndex=0;}
void writeMotorolaTmsServiceAvailability(uint32_t);
bool hasLLIdFNEReg(uint32_t)const;bool processConvDataReg(const uint8_t*);bool handleMotorolaScepArsRegistration(uint32_t,uint32_t);bool handleMotorolaArsRefresh(uint32_t,uint32_t);
#ifdef BASELINE
void writeRF_PDU(const uint8_t*,uint32_t,bool=false,bool=false,bool=true);void writeRF_PDU_User(data::DataHeader&,bool,bool,uint8_t*,bool=false);void writeRF_PDU_Reg_Response(uint8_t,uint32_t,uint32_t);void writeRF_PDU_Ack_Response(uint8_t,uint8_t,uint8_t,uint32_t,bool,uint32_t=0);
#else
void writeRF_PDU(const uint8_t*,uint32_t,bool=false,bool=false,bool=true,ImmediateRfResponse* =nullptr,unsigned=0);
void writeRF_PDU_User(data::DataHeader&,bool,bool,uint8_t*,bool=false,ImmediateRfResponse* =nullptr);void writeRF_PDU_Reg_Response(uint8_t,uint32_t,uint32_t,ImmediateRfResponse* =nullptr);void writeRF_PDU_Ack_Response(uint8_t,uint8_t,uint8_t,uint32_t,bool,uint32_t=0,ImmediateRfResponse* =nullptr);
#endif
};

