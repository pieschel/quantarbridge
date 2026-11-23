# QuantarBridge

QuantarBridge connects a Motorola Quantar in P25 conventional mode directly to
BrandMeister, with direct IMBE/AMBE speech-parameter conversion in both
directions. It combines a patched DVMHost/DVMFNE stack, two DVMBridge
processes, a native BrandMeister voice and packet-data client, Motorola APX
data services, and a local operations dashboard. A prebuilt Raspberry Pi
ARM64 image provides the complete stack. TETRAPACK BREW
remains available as an explicit opt-in migration path.

This repository contains no station credentials, operator data, packet
captures, radio registrations, or private runtime state. Installation creates
a separate runtime directory outside Git.

## Features

- Bidirectional P25/DMR voice directly through BrandMeister and a Quantar DFSI/V.24 connection
- Configurable P25 to BrandMeister talkgroup mapping
- Static and dynamic BrandMeister talkgroups with configurable expiry
- Direct IMBE-to-AMBE and AMBE-to-IMBE conversion with per-call codec reset
- Corrected direct-codec quantization, spectral mapping and call-state handling
- Automatic incoming DMR-to-P25 level adjustment with speech/noise gating and peak protection
- Motorola APX conventional packet-data registration (ARS/SCEP)
- APX Text Messaging Service (TMS), local delivery, and BrandMeister routing
- Motorola LRRP polling and forwarding toward BrandMeister APRS
- Native BrandMeister TMS/LRRP and service-reply routing
- Optional TETRAPACK BREW voice and compatible messaging transport
- Dashboard for registrations, positions, active calls, talkgroups, and service state
- Authenticated administration for network, mapping, audio, GPS, and timeout settings
- Persistent station settings with guarded updates and configuration backups
- Raspberry Pi ARM64 image with automated codec, SSH/sudo and storage checks

## Version 0.1.6

- **P25 to DMR:** corrected gain quantization, spectral bit packing and silence
  handling in the direct converter.
- **DMR to P25:** corrected source-model mapping and stream state, with the
  accepted audio tuning and automatic level adjustment for stations with
  different microphone levels. Noise gating and peak protection limit unwanted
  gain changes and clipping.
- **P25 voice continuity:** registration and ARS responses wait for network
  voice and its queued tail to finish before entering the RF transmit queues.
- **Dashboard:** the direct BrandMeister profile monitors its five required
  services without requiring the unused DMR Gateway. On dashboard startup,
  BrandMeister status can recover the latest timestamped connection event
  from older logs without replaying historical calls.
- The Raspberry Pi image retains the configuration-parser, SSH/sudo and
  filesystem-persistence checks from the previous releases.

The audio settings were evaluated on the running Quantar system. The newly
packaged Raspberry Pi image requires its own station setup and RF checks;
it has not been physically boot-tested on a Raspberry Pi.

See the [release notes](release-notes/v0.1.6.md) and the
[GitHub releases](https://github.com/pieschel/quantarbridge/releases) for
the image, checksum, validation reports and existing-installation caveats.

## Architecture

```text
Motorola APX / P25 RF
          |
       Quantar
          |
       DFSI/V.24
          |
       dvmhost -------- ARS / TMS / LRRP
          |
        dvmfne
       /      \
 P25->DMR      DMR->P25
       \      /
 Native BrandMeister client -------- voice / TMS / LRRP / APRS
```

The DVMHost modifications are distributed as
[`patches/dvmhost.patch`](patches/dvmhost.patch),
[`patches/dvmhost-quantar-rssi.patch`](patches/dvmhost-quantar-rssi.patch),
[`patches/dvmhost-direct-audio.patch`](patches/dvmhost-direct-audio.patch) and
[`patches/dvmhost-immediate-response.patch`](patches/dvmhost-immediate-response.patch)
against commit
`01979084df9fc6a5737fac9efb213430268377c9`. Subscriber radio IDs are learned
from registrations; packet-data addresses are read from the private runtime
configuration generated during installation.

## Documentation

- [Installation](docs/INSTALL.md)
- [Raspberry Pi ARM64 image](docs/RASPBERRY_PI_IMAGE.md)
- [Configuration and architecture](docs/ARCHITECTURE.md)
- [Motorola APX CPS setup](docs/APX_CONFIGURATION.md)
- [Operation and troubleshooting](docs/OPERATIONS.md)
- [Security policy](SECURITY.md)

## Quick Start

Use a dedicated Debian or Ubuntu host with the Quantar V.24 interface attached.
Clone the repository, then run:

```bash
sudo ./scripts/install.sh \
  --bm-id 123456 \
  --bm-callsign N0CALL \
  --bm-master 2622.master.brandmeister.network \
  --rx-frequency 430800000 \
  --tx-frequency 438800000 \
  --serial-port /dev/ttyUSB0
```

The script asks for the BrandMeister device password, dashboard password, and
optional BrandMeister API key without echoing them. Replace every example value
with values assigned to your station before connecting to a live network.

## Important

This is experimental amateur-radio software. Do not use it for emergency,
public-safety, life-safety, or availability-critical communications. Ensure
that your frequencies, IDs, network access, and transmitted content comply
with local law and the policies of every connected network.

QuantarBridge is an independent project and is not affiliated with or endorsed
by Motorola Solutions or BrandMeister.

## License

GPL-2.0-only. The DVMHost patch retains the upstream copyright notices and is
distributed under the same license. Leaflet keeps its own license under
`dashboard/static/vendor/leaflet/LICENSE`. The installer downloads the pinned
external `tetra-codec` source from its upstream repository; it is not vendored
here and remains subject to its upstream notices and terms.
