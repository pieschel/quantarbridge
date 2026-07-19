cmake_minimum_required(VERSION 3.20)
project(quantarbridge LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(DVMHOST_SOURCE_DIR "/home/quantar/src/dvmhost" CACHE PATH "Path to the dvmhost source tree")
set(DVMHOST_COMMON_LIBRARY "/home/quantar/src/dvmhost/build/libcommon.a" CACHE FILEPATH "Path to the prebuilt dvmhost common static library")

if(NOT EXISTS "${DVMHOST_SOURCE_DIR}/CMakeLists.txt")
  message(FATAL_ERROR "DVMHOST_SOURCE_DIR does not point to a dvmhost checkout: ${DVMHOST_SOURCE_DIR}")
endif()
if(NOT EXISTS "${DVMHOST_COMMON_LIBRARY}")
  message(FATAL_ERROR "DVMHOST_COMMON_LIBRARY does not exist: ${DVMHOST_COMMON_LIBRARY}")
endif()

add_executable(quantarbridge
  src/AppConfig.cpp
  src/BMNetwork.cpp
  src/BmPacketDataEncoder.cpp
  src/BmPacketDataReassembler.cpp
  src/SmsSubsystem.cpp
  src/main.cpp
)

target_include_directories(quantarbridge PRIVATE
  "${DVMHOST_SOURCE_DIR}/src"
  "${DVMHOST_SOURCE_DIR}"
  "${CMAKE_CURRENT_SOURCE_DIR}/src"
)

find_package(yaml-cpp REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)

target_link_libraries(quantarbridge PRIVATE "${DVMHOST_COMMON_LIBRARY}" yaml-cpp OpenSSL::SSL OpenSSL::Crypto Threads::Threads)

include(CTest)
if(BUILD_TESTING)
  add_executable(bm_packet_data_reassembler_test
    tests/BmPacketDataReassemblerTest.cpp
    src/BmPacketDataReassembler.cpp
  )
  target_include_directories(bm_packet_data_reassembler_test PRIVATE
    "${DVMHOST_SOURCE_DIR}/src"
    "${DVMHOST_SOURCE_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
  )
  target_link_libraries(bm_packet_data_reassembler_test PRIVATE
    "${DVMHOST_COMMON_LIBRARY}" OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
  add_test(NAME bm_packet_data_reassembler COMMAND bm_packet_data_reassembler_test)

  add_executable(bm_packet_data_encoder_test
    tests/BmPacketDataEncoderTest.cpp
    src/BmPacketDataEncoder.cpp
    src/BmPacketDataReassembler.cpp
  )
  target_include_directories(bm_packet_data_encoder_test PRIVATE
    "${DVMHOST_SOURCE_DIR}/src"
    "${DVMHOST_SOURCE_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
  )
  target_link_libraries(bm_packet_data_encoder_test PRIVATE
    "${DVMHOST_COMMON_LIBRARY}" OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
  add_test(NAME bm_packet_data_encoder COMMAND bm_packet_data_encoder_test)

  add_executable(bm_protocol_test
    tests/BMProtocolTest.cpp
  )
  target_include_directories(bm_protocol_test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
  )
  add_test(NAME bm_protocol COMMAND bm_protocol_test)

  add_executable(sms_brandmeister_tms_test
    tests/SmsSubsystemBrandmeisterTest.cpp
    src/SmsSubsystem.cpp
  )
  target_include_directories(sms_brandmeister_tms_test PRIVATE
    "${DVMHOST_SOURCE_DIR}/src"
    "${DVMHOST_SOURCE_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
  )
  target_link_libraries(sms_brandmeister_tms_test PRIVATE
    "${DVMHOST_COMMON_LIBRARY}" yaml-cpp OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
  add_test(NAME sms_brandmeister_tms COMMAND sms_brandmeister_tms_test)

  find_package(Python3 COMPONENTS Interpreter)
  if(Python3_Interpreter_FOUND)
    add_test(NAME tetrapack_brew_bridge
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/tetrapack_brew_bridge_test.py")
    add_test(NAME tetrapack_brew_audio
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/tetrapack_brew_audio_test.py")
    add_test(NAME enable_brew_audio
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/enable_brew_audio_test.py")
    add_test(NAME quantar_dashboard
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/dashboard_test.py")
    add_test(NAME bm_static_sync
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/bm_static_sync_test.py")
    add_test(NAME ensure_static_tg
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/ensure_static_tg_test.py")
    add_test(NAME dmr_to_p25_recover
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/dmr_to_p25_recover_test.py")
    add_test(NAME quantar_static_recover
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/quantar_static_recover_test.py")
    add_test(NAME bm_to_p25_recover
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/bm_to_p25_recover_test.py")
    add_test(NAME dvmhost_patch
      COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/dvmhost_patch_test.py")
  endif()
endif()
