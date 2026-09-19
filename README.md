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
- Calibrated direct-audio defaults; optional legacy PCM gain/AGC controls
- Motorola APX conventional packet-data registration (ARS/SCEP)
- APX Text Messaging Service (TMS), local delivery, and BrandMeister routing
- Motorola LRRP polling and forwarding toward BrandMeister APRS
- Native BrandMeister TMS/LRRP and service-reply routing
- Optional TETRAPACK BREW voice and compatible messaging transport
- Dashboard for registrations, positions, active calls, talkgroups, and service state
- Authenticated administration for network, mapping, audio, GPS, and timeout settings
- Persistent station settings with guarded updates and configuration backups
- Raspberry Pi ARM64 image with automated codec, SSH/sudo and storage checks

## Version 0.1.4

- **Audio:** both directions use the direct conversion settings confirmed in
  live listening tests on 19 September 2026. Tests cover call-state reset,
  codec boundaries and damaged frames.
- **Saved settings:** the native BrandMeister dashboard no longer tries to
  restart an absent BREW audio target. That old error rolled back changes to
  passwords, talkgroup mappings and timeouts, making settings appear lost
  when the dashboard was loaded again.
- **Raspberry Pi image:** setup rejects volatile/read-only storage, writes
  are flushed to disk, and the SSH welcome text no longer incorrectly says
  that an already configured installation needs setup. The image build
  compares configuration/authentication files across an unmount/remount.

See the [release notes](release-notes/v0.1.4.md) and the
[GitHub releases](https://github.com/pieschel/quantarbridge/releases) for
the image, checksum and validation reports. A physical Raspberry Pi reboot
test of the new image remains to be confirmed; the storage test is not a
hardware boot test.

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
[`patches/dvmhost-quantar-rssi.patch`](patches/dvmhost-quantar-rssi.patch) and
[`patches/dvmhost-direct-audio.patch`](patches/dvmhost-direct-audio.patch)
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
