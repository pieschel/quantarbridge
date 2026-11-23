#!/usr/bin/env python3
"""Exercise real P25 response scheduling methods with deterministic memory leaves.

This is a queue/ownership regression, not a modem, FEC, concurrency or RF test.
No socket, radio, live service or runtime configuration is accessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def extract(text: str, signature: str) -> str:
    start = text.index(signature)
    brace = text.index("{", start)
    end, depth = brace + 1, 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dvmhost-source", required=True, type=Path)
    parser.add_argument("--compiler", default="c++")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    source = args.dvmhost_source.resolve()
    manifest = json.loads((here.parents[1] / "patches/immediate-response-source-manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["source_files"].items():
        contents = (source / name).read_bytes().replace(b"\r\n", b"\n")
        actual = hashlib.sha256(contents).hexdigest()
        if actual != expected:
            raise SystemExit(f"Released P25 guard source differs: {name}")
    cpp = (source / "src/host/p25/packet/Data.cpp").read_text(encoding="utf-8")
    header = (source / "src/host/p25/packet/Data.h").read_text(encoding="utf-8")
    control = (source / "src/host/p25/Control.cpp").read_text(encoding="utf-8")
    signatures = [
        "bool Data::hasLLIdFNEReg(", "bool Data::processConvDataReg(",
        "bool Data::handleMotorolaScepArsRegistration(", "bool Data::handleMotorolaArsRefresh(",
        "void Data::writeMotorolaTmsServiceAvailability(", "void Data::writeRF_PDU_User(",
        "void Data::writeRF_PDU(", "void Data::writeRF_PDU_Reg_Response(",
        "void Data::writeRF_PDU_Ack_Response(", "bool Data::beginImmediateResponse(",
        "void Data::commitImmediateResponse(", "void Data::forgetImmediateRetry(",
        "bool Data::immediateRetryAckMatches(", "bool Data::hasPendingImmediateSession(",
        "void Data::requestImmediateReplay(", "void Data::requestImmediateRetry(",
        "void Data::queueImmediateTimeoutRetry(", "bool Data::serviceImmediateResponses(",
        "bool Data::deferMotorolaDataForNetworkVoice(",
    ]
    bodies = [extract(cpp, signature) for signature in signatures]
    response = cpp[
        cpp.index("                auto findMotorolaResponseSession ="):
        cpp.index("                const auto responseSession = findMotorolaResponseSession(responseLlId);")
    ]
    assert response.count("{") == response.count("}")
    bodies.append("void Data::handleDecodedResponse() {\n" + response + "\n}")
    clock = extract(cpp, "void Data::clock(uint32_t ms)")
    prefix = clock[
        clock.index("    if (serviceImmediateResponses(ms))"):
        clock.index("    for (auto& entry : m_motorolaSessions)")
    ]
    bodies.append("void Data::clockImmediatePrefixForTest(uint32_t ms) {\n" + prefix + "\n++timedWrites;\n}")
    builders = [extract(control, sig) for sig in [
        "void Control::writeRF_TDU(", "void Control::writeRF_Nulls(", "void Control::writeRF_Preamble(",
    ]]
    constants = re.search(r"^const uint8_t MAX_PDU_RETRY_CNT[^;]+;", cpp, re.M).group(0)
    for name in [
        "kMotorolaTmsP25FragmentAckTimeoutMs", "kMotorolaTmsP25FragmentGapMs",
        "kMotorolaTmsApplicationAckTimeoutMs", "kMotorolaTmsServiceRetryDelayMs",
        "kMotorolaTmsP25FragmentRestartDelayMs",
    ]:
        constants += "\n" + re.search(r"^constexpr uint32_t " + name + r"[^;]+;", cpp, re.M).group(0)
    structs = "\n".join(extract(header, sig) + ";" for sig in [
        "enum class MotorolaSessionState", "struct MotorolaSession",
        "struct ImmediateRfPdu", "struct ImmediateRfResponse {",
    ])
    start = header.index("std::recursive_mutex m_immediateResponseMutex;")
    end = header.index("bool m_motorolaNetworkVoiceDrainPending", start)
    with tempfile.TemporaryDirectory(prefix="qb-immediate-response-") as directory:
        output = Path(directory)
        for name in ["test_prelude.hpp", "test_main.cpp"]:
            shutil.copyfile(here / name, output / name)
        (output / "candidate_structs.hpp").write_text(structs, encoding="utf-8")
        (output / "candidate_members.hpp").write_text(header[start:end], encoding="utf-8")
        generated = (
            '#define STRUCTS_HEADER "candidate_structs.hpp"\n'
            '#define MEMBERS_HEADER "candidate_members.hpp"\n'
            '#include "test_prelude.hpp"\n' + constants + "\n" +
            "\n".join(builders + bodies) + '\n#include "test_main.cpp"\n'
        )
        cpp_path, executable = output / "test.cpp", output / "response-test.exe"
        cpp_path.write_text(generated, encoding="utf-8")
        command = [args.compiler]
        if Path(args.compiler).stem == "zig":
            command.append("c++")
        command += ["-std=c++14", "-O1", "-UNDEBUG", "-I", str(output), str(cpp_path), "-o", str(executable)]
        subprocess.run(command, check=True, timeout=120)
        subprocess.run([str(executable)], cwd=output, check=True, timeout=30)


if __name__ == "__main__":
    main()
