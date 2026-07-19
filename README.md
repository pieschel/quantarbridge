# QuantarBridge

QuantarBridge connects a Motorola Quantar in P25 conventional mode to
BrandMeister through the TETRAPACK BREW interface. It combines a patched
DVMHost/DVMFNE stack, two PCM-facing DVMBridge processes, a TETRA speech codec,
a separate BrandMeister packet-data client, Motorola APX data services, and a
local operations dashboard.

This repository contains no station credentials, operator data, packet
captures, radio registrations, or private runtime state. Installation creates
a separate runtime directory outside Git.

## Features

- Bidirectional P25/TETRA voice through BREW and a Quantar DFSI/V.24 connection
- Configurable P25 to BrandMeister talkgroup mapping
- Static and dynamic BrandMeister talkgroups with configurable expiry
- Per-direction audio gain, AGC, and timing controls
- Motorola APX conventional packet-data registration (ARS/SCEP)
- APX Text Messaging Service (TMS), local delivery, and BrandMeister routing
- Motorola LRRP polling and forwarding toward BrandMeister APRS
- TETRAPACK BREW voice, affiliation, and compatible messaging transport
- Dashboard for registrations, positions, active calls, talkgroups, and service state
- Authenticated administration for network, mapping, audio, GPS, and timeout settings

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
 P25->PCM      PCM->P25
       \      /
  BREW audio bridge
          |
 TETRAPACK / BrandMeister

Native BrandMeister client -------- TMS / LRRP / APRS data only
```

The DVMHost modifications are distributed as
[`patches/dvmhost.patch`](patches/dvmhost.patch) and
[`patches/dvmhost-quantar-rssi.patch`](patches/dvmhost-quantar-rssi.patch)
against commit
`01979084df9fc6a5737fac9efb213430268377c9`. Subscriber radio IDs are learned
from registrations; packet-data addresses are read from the private runtime
configuration generated during installation.

## Documentation

- [Installation](docs/INSTALL.md)
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
  --brew-username 123456 \
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
