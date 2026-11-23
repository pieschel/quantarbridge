# P25 response scheduling regression

Run against the pinned DVMHost source after applying the four release patches:

```sh
python3 tests/immediate-response/build_test.py --dvmhost-source /path/to/dvmhost
```

The runner checks the public source manifest, extracts the real response queue,
registration, paired ARS, ACK/retry and voice/tail scheduler methods, and executes
43 synthetic cases with 351 assertions. The cases cover bounded admission,
duplicate coalescing, owned retries, stale ACKs, pending sessions, complete frame
bundles and pauses while network voice or either host queue remains busy.

Parser, assembler, FEC, modem and lookup leaves are deterministic memory stubs.
The harness does not test RF decoding, physical transmitter timing or concurrent
execution. It does not open sockets or access live services or configurations.
It creates generated files only in a temporary directory.

The guard requires `protocols.p25.repeatDataPacket: false`, as supplied by the
image. It covers immediate conventional registration and recognized paired
SCEP/ARS responses, their owned retries and the existing timed voice guard; it
does not gate every generic PDU writer.
