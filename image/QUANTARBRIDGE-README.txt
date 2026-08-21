QuantarBridge Raspberry Pi ARM64 image
======================================

This image contains prebuilt ARM64 versions of QuantarBridge, the pinned
DVMHost stack, and optional tetra-codec. Private radio, BrandMeister, dashboard,
and location settings are deliberately not embedded.

Supported image target
----------------------

- Raspberry Pi OS Lite 64-bit (Debian 13 / trixie)
- Raspberry Pi 3/3+/4/5, 400, Zero 2 W, and compatible Compute Modules
- Raspberry Pi 4 or 5 recommended
- Motorola Quantar V.24/DFSI interface through a serial adapter

Default first login
-------------------

Write the .img.xz file to an SD card and connect Ethernet before first boot.
The public initial SSH credentials are:

    Username: qbadmin
    Password: quantarbridge

Run passwd immediately after the first login and replace the public initial
password. Do not expose the unconfigured device to an untrusted network.

First configuration
-------------------

1. Connect the Quantar serial adapter, normally exposed as /dev/ttyUSB0.
2. Boot, log in as qbadmin, and immediately run passwd.
3. Run:

       sudo quantarbridge-setup

4. Enter the assigned repeater, frequency, P25, and BrandMeister values.
   Voice, TMS, and LRRP use the direct BrandMeister connection by default.
5. Verify voice and packet data in both directions before enabling watchdogs.

The setup command stores credentials only under
/home/quantar/quantar-runtime with restrictive permissions. To reconfigure,
run "sudo quantarbridge-setup --reconfigure"; it creates a root-only backup.

This is not a Pi-Star/MMDVM_HS GPIO-Hat image. That radio path requires a
different DVMHost modem configuration and separate end-to-end RF validation.
