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
  tetrapack-brew-audio.service \
  quantarbridge.service \
  quantar-dashboard.service
```

Follow logs with:

```bash
journalctl -fu dvmhost.service
journalctl -fu dvmfne.service
journalctl -fu quantarbridge.service
tail -F /home/quantar/quantar-runtime/log/tetrapack-brew-audio.log
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

The dashboard exposes separate settings for P25-to-BREW and BREW-to-P25.
Changes that require a service restart are accepted only after 15 seconds of
continuous radio-channel idle time. This prevents a short gap within a QSO from
being mistaken for a safe restart window. Audio changes restart only the
affected stateless PCM bridge and, when its gain changed, the BREW audio worker.
They do not restart DVMFNE or DVMHost, so APX registrations remain intact.

| Setting | Effect |
| --- | --- |
| `rxAudioGain` | Input level before the direction-specific decode path |
| `vocoderDecoderAudioGain` | Level after vocoder decoding |
| `vocoderDecoderAutoGain` | Automatic decoder gain; can pump on noisy sources |
| `vocoderDecoderUvQuality` | Internal decoder synthesis quality |
| `txAudioGain` | Final level into the destination encoder/RF path |
| `vocoderEncoderAudioGain` | Level immediately before vocoder encoding |
| `p25EncodePresenceGain` | BREW-to-P25 high-frequency emphasis; excessive values can sound scratchy |
| `p25EncodeHighCutHz` | Optional BREW-to-P25 low-pass before IMBE; `0` disables it |
| `p25EncodeAgcPeakLimit` | Absolute BREW-to-P25 PCM ceiling after final gain, with or without AGC |
| `dropTimeMs` | Tail time before a call is released |

Raise one gain at a time. If peaks become scratchy while average loudness is
correct, reduce the last gain before the encoder and use a smaller upstream
increase. Different subscriber microphones can still produce different peak
levels.

The shipped BREW-to-P25 baseline is the known-good profile tuned on the reference
Quantar installation: worker `downlinkGain: 1.0`, `rxAudioGain: 0.3`,
`vocoderDecoderAudioGain: 0.4`, decoder AGC off,
`vocoderDecoderUvQuality: 12`, `txAudioGain: 1.10`,
`vocoderEncoderAudioGain: 0.0`, presence boost off, a `2500 Hz` high-cut,
P25 AGC off, and a final peak limit of `24000`. Keep these values together when
restoring the baseline; changing one stage can move clipping into the next codec.

The shipped P25-to-BREW baseline is likewise tuned on the reference installation:
`rxAudioGain: 1.0`, `vocoderDecoderAudioGain: 0.4`, decoder AGC off,
`vocoderDecoderUvQuality: 3`, and worker `uplinkGain: 2.0` with a PCM ceiling of
`24000`. The lower decoder gain provides headroom before the TETRA encoder; the
worker gain restores the required network level after P25 decoding.

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
password. Restart both `tetrapack-brew-audio.service` and the TMS queue adapter
after such a change.

Keep both runtime files outside Git. The BREW audio worker owns the sole
Basestation WebSocket and also transmits queued service TMS. A second process
trying to open the same session can receive HTTP `403` even when the password is
correct. Check `sms/brew-audio-outbox`, `sms/brew-audio-results`, and the
`brewSmsCommandsSent` status counter before changing credentials.

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

Ordinary dynamic expiry sends a BREW de-affiliation and updates
`dynamic_routes.state`; it does not restart a service. The legacy direct-audio
recovery timers and `quantar-static-recover.path` stay disabled.

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
