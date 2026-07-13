# Motorola APX CPS Configuration

This guide describes the tested conventional P25 FNE path. CPS labels vary by
APX generation, firmware, region, and entitlement. A field that is absent or
read-only usually indicates a feature-set limitation rather than a server error.

## Required Radio Features

At minimum, verify the radio feature report contains:

- P25 Common Air Interface
- Packet Data Interface (`Q947` on tested radios)
- Text Messaging capability
- GPS activation when LRRP/APRS is required

`Enhanced Data Operation` (`QA03399` on tested feature reports) may be required
by some APX generations for enhanced server modes or for accepting arbitrary
server addresses. The basic bridge uses Conventional + FNE + ARS `Server`; it
does not require `Enhanced Server` for periodic LRRP polling.

Do not enable encrypted packet data unless the complete FNE path is configured
for the matching feature and keys.

## Values You Need

Prepare these site-specific values before opening CPS:

| Value | Example only |
| --- | --- |
| APX radio ID | `1000001` |
| ARS server IP | `10.0.0.2` |
| P25 NAC | `293` hex |
| P25 system ID | `001` hex |
| P25 network ID | `BB800` hex |
| RX/TX frequencies | assigned site frequencies |

The APX radio ID must be unique. The ARS server IP must exactly match
`protocols.p25.motorolaPacketData.arsServerAddress` in the private
`dvmhost-config.yml` generated during installation.

## 1. Radio-Wide Data

Open `Data Configuration > Data Wide`.

1. Enable direct TMS content display if messages should appear directly in the inbox.
2. Leave External Text Messaging Broadcast disabled unless a separate application requires it.
3. Set a unique Subscriber IP only when the radio does not use automatic generation.
4. Keep the peer assignment dynamic for the conventional FNE profile.
5. Leave NAT disabled for the normal RF data path.

Typical port values on tested APX radios are:

- Authentication UDP: `49165`
- P25 Location Reporting UDP: `49198`

Do not change model-specific defaults merely to match these examples.

## 2. Data Profile

Create a profile under `Data Configuration > Data Profiles`:

| CPS field | Setting |
| --- | --- |
| Data Profile Type | `Conventional` |
| Packet Data Mode | `FNE` |
| Auto Generate IP Address | enabled |
| Auto Generate Target IP Address | enabled |
| Terminal Data | enabled when available |
| ARS Mode | `Server` |
| Automatic Registration Server Address | site ARS server IP |
| Direct Location Registration | disabled for normal LRRP polling |
| Packet Data Security | clear |

Start with the CPS defaults for retry timers and header compression. The bridge
does not require a custom PAD sequence.

If CPS rejects a private ARS address while another syntactically valid address
is accepted, compare the radio feature report with a working unit. Packet Data
Interface alone may not unlock Enhanced Data Operation on every APX generation.

## 3. ASTRO System and Conventional Personality

1. Create or select the ASTRO system used by the Quantar.
2. Set the radio ID assigned to this APX.
3. Configure matching NAC, System ID, Network ID, and modulation.
4. Create a conventional personality with RX and TX Voice/Signal Type `ASTRO`.
5. Assign the Data Profile from the previous section.
6. Enable CAI Data Registration or the equivalent Conventional Data Registration option.
7. Enable automatic ARS registration for the channel/personality.
8. Assign the personality to the required zone and channel.

Voice working on the channel does not prove packet data is configured. The
registration and TMS settings are separate from the P25 voice path.

## 4. TMS

Enable the Text Messaging menu and any required one-touch or menu entries. A
quick-text list is optional. Target addresses are radio IDs, not talkgroups.

After selecting the channel, wait for data registration. A black-backed IP icon
on tested APX displays indicates an active packet-data context. It is not a
permanent TCP-style connection indicator.

For the first test:

1. Send a short message between two locally registered APX radios.
2. Send a short message from APX to a known BrandMeister text service.
3. Send from a BrandMeister-connected DMR radio to the APX radio ID.
4. Test a message longer than one RF block and verify that it appears as one inbox item.

`Message Sent` confirms the FNE accepted and acknowledged the APX submission.
It does not by itself confirm delivery by an external service.

## 5. GPS and LRRP

Enable GPS and location reporting in the radio-wide and personality settings.
The normal bridge mode polls the APX from the FNE, so `Send Location to Peer/on
PTT` is not needed. Some CPS versions force ARS to `Enhanced Server` when that
option is enabled; leave it off unless the radio has the required Enhanced Data
feature and the site intentionally uses that mode.

Server polling intervals are configured in `dvmhost-config.yml`:

```yaml
protocols:
  p25:
    motorolaLocation:
      initialDelaySeconds: 5
      updateIntervalSeconds: 300
      noFixRetrySeconds: 60
```

## Troubleshooting

### Immediate `Send Failed`

- Confirm the IP icon is active.
- Confirm the radio logged a new ARS registration after the latest `dvmhost` restart.
- Verify the Data Profile is assigned to the active conventional personality.
- Verify ARS Mode is `Server` and the server IP matches `dvmhost-config.yml`.
- Switch briefly to another channel and back to force one clean registration.

### IP icon never becomes active

- Check that the Quantar passes P25 packet data, not voice only.
- Check `Q947` and the TMS entitlement in the feature report.
- Verify the APX radio ID, NAC, Network ID, and System ID.
- Inspect `journalctl -u dvmhost.service` for ARS and SNDCP activity.

### Registration works but no inbound text arrives

- Confirm the target APX remains registered and TMS is marked available.
- Check `/home/quantar/quantar-runtime/sms/p25-outbox` and `sms/error`.
- Verify all confirmed RF fragments are acknowledged.
- Test a short local message before testing an external network service.

### Multiple APX radios

Every radio needs its own radio ID and generated subscriber IP. Reusing either
can make registration appear successful while replies are routed to the wrong
session.
