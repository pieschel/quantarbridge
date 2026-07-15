# Operation and Troubleshooting

## Dashboard

Open `http://<bridge-address>:8088/` on the management network. Status is
read-only without login. Administration requires the account created during
installation.

The dashboard shows:

- Core service state
- Registered APX radios and last packet-data activity
- Current and recent calls in `mm:ss`
- Source radio and talkgroup names resolved from BrandMeister
- Static and dynamic subscriptions, including dynamic expiry
- Last LRRP position for each radio
- Public ARS server address and bidirectional talkgroup mappings

The ARS address is read from `protocols.p25.motorolaPacketData.arsServerAddress`
when that key exists. Installations whose DVMHost configuration does not expose
that key should set `publicArsServerAddress` in the private
`quantar-dashboard.json`. A successfully observed ARS registration remains the
runtime fallback and is not cleared when the administration page reloads its
settings.

## Core Services

```bash
systemctl status \
  dvmfne.service \
  dvmhost.service \
  dvmbridge-p25-to-dmr.service \
  dvmbridge-dmr-to-p25.service \
  quantarbridge.service \
  quantar-dashboard.service
```

Follow logs with:

```bash
journalctl -fu dvmhost.service
journalctl -fu dvmfne.service
journalctl -fu quantarbridge.service
```

## First Voice Test

1. Verify all FNE peers complete login and configuration exchange.
2. Key a short P25 group call and confirm an uplink appears in the dashboard.
3. Generate a BrandMeister downlink and confirm P25 RF audio.
4. Check that the P25 and BrandMeister talkgroups are mapped in both directions.

If an RF call appears in the dashboard but no dynamic talkgroup is created,
compare the `inclusion` IDs in `talkgroup_rules.yml` with the `network.id` and
`fne.peerId` values of all four local peers. The static-sync job derives this list
from the runtime YAML files and then reconnects both DVMBridge directions.

Do not tune gain while a recovery timer is restarting services. Fix service
stability first, then compare the same source audio in both directions.

## Audio Controls

The dashboard exposes separate settings for P25-to-DMR and DMR-to-P25.
Changes that require a service restart are accepted only after 15 seconds of
continuous radio-channel idle time. This prevents a short gap within a QSO from
being mistaken for a safe restart window. Audio changes also reset the local
FNE router first and then reconnect the affected transcoder, so stale peer
routing cannot leave either direction silently disconnected. Both DVMBridge
units also follow every direct `dvmfne.service` restart through systemd.

| Setting | Effect |
| --- | --- |
| `rxAudioGain` | Input level before the direction-specific decode path |
| `vocoderDecoderAudioGain` | Level after vocoder decoding |
| `vocoderDecoderAutoGain` | Automatic decoder gain; can pump on noisy sources |
| `txAudioGain` | Final level into the destination encoder/RF path |
| `vocoderEncoderAudioGain` | Level immediately before vocoder encoding |
| `dropTimeMs` | Tail time before a call is released |

Raise one gain at a time. If peaks become scratchy while average loudness is
correct, reduce the last gain before the encoder and use a smaller upstream
increase. Different subscriber microphones can still produce different peak
levels.

## TMS Test Sequence

1. Re-register the APX after any intentional `dvmhost` restart.
2. Verify a log line for ARS registration and TMS availability.
3. Send APX to a second local APX.
4. Send APX to BrandMeister.
5. Send BrandMeister to APX.
6. Test a long response and confirm one reconstructed inbox message.

Queue directories:

```text
/home/quantar/quantar-runtime/sms/inbox
/home/quantar/quantar-runtime/sms/outbox
/home/quantar/quantar-runtime/sms/p25-outbox
/home/quantar/quantar-runtime/sms/service-routes
/home/quantar/quantar-runtime/sms/processed
/home/quantar/quantar-runtime/sms/error
```

An immediate APX `Send Failed` usually happens before the message reaches the
queue. Start with ARS/TMS session state rather than the BrandMeister transport.

### TETRAPACK BREW credentials

When BREW delivery is enabled, configure its private SSID username in
`tetrapack-brew-bridge.json`. Leave `brew.password` empty to load the current
BrandMeister hotspot password from the private `quantarbridge.yml` at service
startup. This avoids leaving a stale second copy after changing the device
password. Restart `tetrapack-brew-bridge.service` after such a change.

Keep both runtime files outside Git. An HTTP `401` or `403` from the BREW
endpoint is an authentication failure, not an APX delivery failure. The bridge
records it once as `brew_authentication_rejected` and does not retry the same
message indefinitely.

## LRRP and APRS

After TMS becomes available, DVMHost sends the first LRRP request after
`initialDelaySeconds`. A valid fix uses `updateIntervalSeconds`; an unavailable
or inaccurate fix uses `noFixRetrySeconds`.

Check for `motorola_lrrp` result files under `sms/processed`. A successful local
decode does not guarantee immediate visibility on an external APRS map; the
BrandMeister location path must also accept the packet.

## Dynamic Talkgroups

The configured timeout is `routing.dynamicTimeoutSeconds`. P25 TG `4000`
clears the active dynamic route. Static subscriptions are synchronized from the
BrandMeister device profile by `bm-static-sync.timer`.

Only local P25 RF activity refreshes the timer. The log records `Updated dynamic
TG ... from RF activity`; incoming BrandMeister traffic must not move the expiry.
The dashboard expiry follows the most recent RF timestamp.

Ordinary dynamic expiry restarts only the stateless DMR-to-P25 bridge. It must
not restart `dvmhost`, because that would discard APX packet-data sessions.
`quantar-static-recover.path` is the sole expiry-event handler; it clears the
BrandMeister dynamic route before the configured static route is ensured.

## Backups

Back up the runtime directory with mode-preserving tools while services are
stopped:

```bash
sudo systemctl stop quantarbridge.service dvmhost.service
sudo tar -czf /root/quantar-runtime-$(date +%F).tar.gz \
  -C /home/quantar quantar-runtime
sudo systemctl start dvmhost.service quantarbridge.service
```

The archive contains credentials and must be protected accordingly.

## Common Faults

### Voice works, all TMS functions fail

Check the `dvmhost` start timestamp. If it changed after the APX registered,
force one new radio registration. Then verify no recovery unit is repeatedly
restarting the host.

### BrandMeister rejects the connection

- Use the six-digit repeater ID assigned to the station for repeater mode.
- Ensure the callsign exactly matches the BrandMeister/RadioID profile.
- Use the device password configured for that repeater.
- Verify RX/TX frequency and master selection.

### Dashboard shows stale radios

The dashboard derives state from current logs and identity caches. A graceful
P25 data deregistration removes the radio immediately. If a radio loses power
before its disconnect reaches the FNE, it remains visible until the configured
registration idle period expires. This does not create a new ARS session in
DVMHost.

### Host restart loop

Inspect:

```bash
systemctl list-timers --all | grep -E 'dvmhost|p25|static'
journalctl --since '-15 min' \
  -u dvmhost-recover.service \
  -u dmr-to-p25-recover.service \
  -u bm-to-p25-recover.service
```

Only `dvmhost-recover.service` should restart the stateful host, and only after
its stricter watchdog thresholds are met.
