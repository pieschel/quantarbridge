# Raspberry Pi ARM64 Image

The image build uses the official Raspberry Pi OS Lite 64-bit release and
builds QuantarBridge, the pinned DVMHost revision, and the pinned
`tetra-codec` revision natively on an ARM64 GitHub-hosted runner.

Tagged builds are also published as GitHub release assets and as an OCI
artifact in the GitHub Container Registry. Pull a packaged release with ORAS:

```bash
oras pull ghcr.io/pieschel/quantarbridge-rpi-image:v0.1.5
```

## Target

- Raspberry Pi OS Lite 64-bit, Debian 13 (`trixie`), 2026-06-18
- Raspberry Pi 3/3+/4/5, 400, Zero 2 W, and compatible Compute Modules
- Raspberry Pi 4 or 5 recommended for the complete bridge stack
- Motorola Quantar V.24/DFSI interface through a supported serial adapter

The official compressed base image SHA-256 is
`acff736ca7945e3b305f07cda4abdb870910e12634991da69783611756e381b3`.
The build refuses to continue when that checksum or the resulting ARM64
binary architecture does not match.

## Build

Run the **Raspberry Pi ARM64 image** workflow. The workflow publishes an
artifact containing:

- the flashable `.img.xz` file;
- the image SHA-256 file;
- base and QuantarBridge revision metadata;
- architecture checks for QuantarBridge, DVMHost, and `tetra-codec`.

The image contains no station identity, frequencies, network passwords, API
keys, location, radio activity, or copied production runtime state. It does
contain the public bootstrap login `qbadmin` / `quantarbridge`; that password
must be changed immediately after the first login with `passwd`.

## Install and configure

1. Write and verify the `.img.xz` with Raspberry Pi Imager and connect Ethernet.
2. Boot and connect with `ssh qbadmin@raspberrypi.local` using the initial
   password `quantarbridge`.
3. Run `passwd` immediately and replace the public initial password.
4. Attach the Quantar serial interface and run `sudo quantarbridge-setup`.
5. Enter the assigned station, P25, and BrandMeister settings. Direct
   BrandMeister voice and packet data are the default; BREW is not required.
6. Verify voice, ARS, TMS, and LRRP in that order before enabling recovery
   watchdogs.

Services remain disabled until `quantarbridge-setup` completes. A failed
hardware start does not delete the generated configuration; inspect
`systemctl --failed` and the boot journal after connecting the serial adapter.

## Persistence and reboot troubleshooting

Station configuration, dashboard authentication and setup state are stored
under `/home/quantar/quantar-runtime` on the writable root filesystem.
Do not enable Raspberry Pi OS overlay/read-only mode after setup if you
want later configuration changes to survive reboot. Setup checks the
filesystem before accepting credentials.

In v0.1.3, changing the BM password, talkgroup mappings or dynamic timeout
could fail because the native profile still tried to restart the absent
BREW audio target. That error rolled settings back while leaving the
separately managed dashboard login password intact. v0.1.4 fixes this path.
After saving, reload the settings page and confirm the saved values.

The old SSH welcome text always said "not configured" and was not a test
of saved settings. Do not rerun setup with `--reconfigure` merely because
of that old text. Ordinary setup refuses to overwrite existing files.

The image build validates configuration and login-file hashes across an
unmount/remount and reads settings in a fresh process. These tests do not
replace a physical Pi reboot test. If settings still disappear, preserve
the runtime files and dashboard journal before reconfiguration, then check
`findmnt -T /home/quantar/quantar-runtime` and
`journalctl -b -u quantar-dashboard.service`. Do not publish runtime files:
they contain station credentials.

Direct audio defaults are `directImbeToAmbe: true` with
`directImbeGainAdjust: 1.58` on P25 to DMR and `directAmbeToImbe: true` with
`directAmbeSpectralScale: 4.0`, `directAmbeGainAdjust: 1.0` on DMR to P25.
The dashboard's PCM gain controls apply to the older PCM conversion path.

## Boundary

This image packages the existing Quantar V.24/DFSI topology. It does not turn
an MMDVM_HS GPIO Hat into a drop-in Quantar replacement. That path needs a
different modem configuration and separate bidirectional RF validation.

## v0.1.5 startup repair

Setup now emits DVM-compatible list indentation and omits empty optional key
fields. Static-talkgroup synchronization keeps empty lists as `[]`; the
dashboard accepts older null lists. The ARM64 build validates generated and
remounted configuration with the actual DVM parser. See
[the release notes](../release-notes/v0.1.5.md) for verification boundaries and
existing-installation guidance.
