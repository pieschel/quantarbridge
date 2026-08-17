# Raspberry Pi ARM64 Image

The image build uses the official Raspberry Pi OS Lite 64-bit release and
builds QuantarBridge, the pinned DVMHost revision, and the pinned
`tetra-codec` revision natively on an ARM64 GitHub-hosted runner.

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

The image contains no station identity, frequencies, passwords, API keys,
location, radio activity, or copied production runtime state.

## Install and configure

1. In Raspberry Pi Imager, choose **Use custom** and select the `.img.xz`.
2. Use OS customisation to create a unique administrator login, configure the
   network and locale, and enable SSH.
3. Write and verify the card, attach the Quantar serial interface, and boot.
4. Log in and run `sudo quantarbridge-setup`.
5. Enter the assigned station, P25, BrandMeister, and BREW settings.
6. Verify voice, ARS, TMS, and LRRP in that order before enabling recovery
   watchdogs.

Services remain disabled until `quantarbridge-setup` completes. A failed
hardware start does not delete the generated configuration; inspect
`systemctl --failed` and the boot journal after connecting the serial adapter.

## Boundary

This image packages the existing Quantar V.24/DFSI topology. It does not turn
an MMDVM_HS GPIO Hat into a drop-in Quantar replacement. That path needs a
different modem configuration and separate bidirectional RF validation.
