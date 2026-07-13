# Architecture and Configuration

## Components

| Component | Purpose |
| --- | --- |
| `dvmhost` | Terminates Quantar DFSI/V.24 and owns P25 RF, ARS, TMS, and LRRP state |
| `dvmfne` | Local fixed-network core and peer router |
| `dvmbridge-p25-to-dmr` | Decodes P25 voice and emits the DMR-side audio stream |
| `dvmbridge-dmr-to-p25` | Encodes BrandMeister downlink audio for P25 RF |
| `quantarbridge` | BrandMeister protocol, call routing, talkgroup mapping, and packet-data transport |
| `tetrapack_brew_bridge.py` | Optional TMS queue adapter and BREW transport |
| `dashboard/app.py` | Read-only operations view plus authenticated administration |

All internal network and UDP audio links use loopback by default. Only the
BrandMeister master connection and dashboard listener need external network
access.

## Default Local Peer IDs

The example configuration uses IDs that are local to the FNE and are not a
substitute for an assigned BrandMeister repeater ID:

| ID | Role |
| --- | --- |
| `9000100` | DVMFNE master |
| `9000101` | QuantarBridge peer |
| `9000110` | DVMHost peer |
| `9000111` | P25 to DMR bridge |
| `9000112` | DMR to P25 bridge |
| `9000199` | Internal transcoder source fallback |

These local IDs are part of the public defaults and therefore must match
`peer_list.dat`, `talkgroup_rules.yml`, and all four peer configurations. Keep
the defaults unless they collide with another local FNE.

## Site-Specific APX Values

The public patch does not contain a station subscriber ID. APX radio IDs and
their generated subscriber addresses are learned from live registrations. The
ARS server IP (default `10.0.0.2`) and compatibility peer IP (default
`10.0.0.1`) are written to private `dvmhost-config.yml` runtime configuration
by the installer.

Every APX still needs a unique, valid radio ID in its codeplug. Never reuse the
example IDs from tests or documentation on air.

## Voice Routing

P25 RF calls enter `dvmhost`, traverse `dvmfne`, and are decoded by the
P25-to-DMR bridge. `quantarbridge` sends the resulting DMR stream to
BrandMeister. Downlink follows the reverse route through the DMR-to-P25 bridge.

`routing.talkgroupMappings` is bidirectional. Example:

```yaml
routing:
  talkgroupMappings:
    - p25: 101
      brandmeister: 262000
```

An RF call to P25 TG `101` is sent to BrandMeister TG `262000`; a BrandMeister
call to `262000` is transmitted on P25 TG `101`.

## Dynamic Talkgroups

An outgoing P25 group call creates a dynamic BrandMeister route. The route
expires after `routing.dynamicTimeoutSeconds`. P25 TG `4000` is the default
disconnect command. Static subscriptions are synchronized from the configured
BrandMeister device profile.

Recovery jobs restart only stateless bridge components for ordinary routing
faults. `dvmhost` is restarted only by its dedicated watchdog after stronger
evidence, because restarting it drops all in-memory ARS, TMS, and LRRP sessions.

## Packet Data

An APX registers through conventional P25 packet data. DVMHost assigns and
tracks the subscriber IP, answers ARS, announces TMS availability, and then
accepts or delivers confirmed packet-data fragments.

```text
APX -> Quantar -> dvmhost -> SMS queue -> QuantarBridge/BREW/BrandMeister
APX <- Quantar <- dvmhost <- P25 outbox <- local or network message
```

Long text messages are split into confirmed P25 blocks. TMS message IDs and RF
sequence numbers let the APX reconstruct one application message rather than
displaying each RF block separately.

LRRP requests are sent only after a TMS-capable session is ready. Valid reports
are forwarded as BrandMeister location packet data. A no-fix response uses the
shorter retry interval from `motorolaLocation.noFixRetrySeconds`.

## Runtime Layout

```text
/home/quantar/quantarbridge        installed source and binaries
/home/quantar/src/dvmhost          pinned and patched DVMHost checkout
/home/quantar/quantar-runtime      configuration, secrets, queues, and logs
```

The runtime directory is deliberately outside the Git checkout. Templates live
under `deploy/examples`; do not run services directly from those templates.

## Important Ports

| Port | Scope | Purpose |
| --- | --- | --- |
| `62031/UDP` | loopback + outbound | Local FNE and BrandMeister protocol |
| `31011/UDP` | loopback | DMR to P25 audio/metadata |
| `31012/UDP` | loopback | P25 to DMR audio/metadata |
| `4005/UDP` | loopback | Host ARS packet-data adapter |
| `4007/UDP` | loopback | Host TMS packet-data adapter |
| `4015/UDP` | loopback | QuantarBridge ARS input |
| `4017/UDP` | loopback | QuantarBridge TMS input |
| `8088/TCP` | management LAN | Dashboard |

Keep these ports firewalled. Port reuse and direction are defined in the
generated runtime files.
