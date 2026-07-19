# Architecture and Configuration

## Components

| Component | Purpose |
| --- | --- |
| `dvmhost` | Terminates Quantar DFSI/V.24 and owns P25 RF, ARS, TMS, and LRRP state |
| `dvmfne` | Local fixed-network core and peer router |
| `dvmbridge-p25-to-dmr` | Compatibility unit name for the P25-to-PCM decoder |
| `dvmbridge-dmr-to-p25` | Compatibility unit name for the PCM-to-P25 encoder |
| `tetrapack_brew_audio.py` | TETRA codec, BREW group calls, affiliations, mapping, and dynamic routes |
| `quantarbridge` | Native BrandMeister session for packet data and management; group voice disabled |
| `tetrapack_brew_bridge.py` | TMS queue adapter for local, BrandMeister packet-data, and BREW delivery |
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
| `9000111` | P25 to PCM bridge |
| `9000112` | PCM to P25 bridge |
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

P25 RF calls enter `dvmhost`, traverse `dvmfne`, and are decoded to 8 kHz PCM.
`tetrapack_brew_audio.py` encodes that PCM with the pinned TETRA codec and sends
a BREW group call. Downlink follows the reverse route from BREW/TETRA to PCM and
then through the P25 encoder. No AMBE/DMR codec remains in the group-voice path.

The native `quantarbridge` BrandMeister connection stays logged in for private
packet data, TMS, LRRP/APRS, device metadata, and dashboard integration. Its
`brandmeister.voiceEnabled` setting is `false`, preventing duplicate group
audio and loops.

TETRAPACK permits one Basestation session for the configured bridge ISSI. The
audio worker owns that WebSocket. TMS requests for BREW services are written to
`sms/brew-audio-outbox` and sent by the audio worker on the same session; the
standalone TMS adapter must not open a competing BREW connection.

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

An outgoing P25 group call creates a BREW affiliation and dynamic route. The
route expires after `routing.dynamicTimeoutSeconds`. P25 TG `4000` is the
default disconnect command. Static affiliations are synchronized from the
configured BrandMeister device profile.

The static-sync job rebuilds the FNE group-voice rule from the peer IDs in the
four active runtime YAML files. Example peer IDs must never be written over a
site-specific FNE configuration, because an unmatched inclusion list prevents
RF voice from reaching either transcoder while all processes still look active.

Only outgoing P25 RF group activity refreshes an active dynamic route. Incoming
BrandMeister traffic never extends the configured timeout. The dashboard uses
the same most-recent RF timestamp as the router.

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

Some external messaging services return replies to the bridge subscriber ID
instead of the requesting APX ID. Before such a request is sent, the queue
stores an expiring FIFO route under `sms/service-routes`. QuantarBridge consumes
exactly one matching route when the service reply arrives and delivers it to
the original requester while keeping the BrandMeister acknowledgement addressed
to the bridge subscriber.

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
| `31120/UDP` | loopback | P25 decoder PCM to BREW audio worker |
| `31121/UDP` | loopback | BREW audio worker PCM to P25 encoder |
| `4005/UDP` | loopback | Host ARS packet-data adapter |
| `4007/UDP` | loopback | Host TMS packet-data adapter |
| `4015/UDP` | loopback | QuantarBridge ARS input |
| `4017/UDP` | loopback | QuantarBridge TMS input |
| `8088/TCP` | management LAN | Dashboard |

Keep these ports firewalled. Port reuse and direction are defined in the
generated runtime files.
