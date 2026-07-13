# Installation

This guide installs the complete QuantarBridge stack on a dedicated Debian or
Ubuntu host. Configuration, credentials, queues, and operational state are
created under `/home/quantar/quantar-runtime` and never belong in the Git
checkout.

## 1. Before You Begin

Prepare the following:

- A Motorola Quantar configured for conventional P25 and connected through a
  supported V.24/DFSI serial interface
- A current Debian or Ubuntu system with `systemd`, Internet access for the
  initial build, and at least 4 GB of free disk space
- The serial device path, normally `/dev/ttyUSB0`
- An assigned six-digit BrandMeister repeater ID and matching callsign
- The BrandMeister master hostname and device password for that repeater
- Licensed frequencies and the P25 NAC, Network ID, and System ID for the site
- A strong, unique password for the local dashboard administrator

Create the repeater in the BrandMeister Sysop Dashboard before installation.
The ID, callsign, frequencies, and device password entered here must agree with
that profile. BrandMeister documents repeater onboarding and per-device
passwords in its official guides:

- <https://help.brandmeister.network/repeaters/connecting-repeaters/>
- <https://help.brandmeister.network/repeaters/sysop-dashboard/device-password/>

Do not reuse a BrandMeister account password as the device password or as the
dashboard password.

## 2. Prepare the Host

Install a minimal supported OS, apply updates, and confirm the serial adapter:

```bash
sudo apt update
sudo apt full-upgrade -y
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

Avoid running another modem host against the same serial port. The installer
creates the unprivileged service account `quantar` and grants it access to the
`dialout` group.

## 3. Clone and Install

```bash
git clone https://github.com/pieschel/quantarbridge.git
cd quantarbridge
sudo ./scripts/install.sh \
  --bm-id 123456 \
  --bm-callsign N0CALL \
  --bm-master 2622.master.brandmeister.network \
  --rx-frequency 430800000 \
  --tx-frequency 438800000 \
  --serial-port /dev/ttyUSB0 \
  --p25-nac 293 \
  --p25-network-id BB800 \
  --p25-system-id 001 \
  --latitude 0.0 \
  --longitude 0.0 \
  --height 0 \
  --power 25 \
  --location "Example Site"
```

All values above are placeholders. Use only IDs, frequencies, and location
details assigned to your station.

The installer prompts without echo for:

1. BrandMeister device password
2. A new dashboard administrator password of at least 12 characters
3. An optional BrandMeister API key used to synchronize talkgroups and recover
   the static route after a dynamic talkgroup expires

No password is accepted as a command-line argument. The installer generates a
random local FNE password, builds the pinned DVMHost revision with the supplied
patch, builds QuantarBridge, runs the test suite, and installs the `systemd`
units.

Use `--dashboard-listen 0.0.0.0` only when the management LAN is trusted and a
host firewall limits TCP port `8088`. The default `127.0.0.1` keeps the
dashboard local to the bridge host. Run `sudo ./scripts/install.sh --help` for
the complete option list.

Recovery watchdogs are deliberately disabled on first installation. Enable
them only after stable voice and packet-data tests:

```bash
sudo systemctl enable --now \
  dvmhost-recover.timer \
  dmr-to-p25-recover.timer \
  bm-to-p25-recover.timer
```

## 4. Verify the Installation

```bash
systemctl --no-pager --full status \
  dvmfne.service \
  dvmhost.service \
  dvmbridge-p25-to-dmr.service \
  dvmbridge-dmr-to-p25.service \
  quantarbridge.service \
  tetrapack-brew-bridge.service \
  quantar-dashboard.service

journalctl -b -u dvmhost.service -u quantarbridge.service --no-pager
```

Expected startup sequence:

1. `dvmfne` listens on the local master port.
2. `dvmhost`, both DVMBridge directions, and QuantarBridge authenticate as
   local peers.
3. QuantarBridge logs in to the configured BrandMeister master.
4. The dashboard responds on the configured management address.

Open `http://<bridge-address>:8088/` and sign in as `admin` with the password
created during installation. There is no built-in or published default
password.

## 5. Configure Routing and Audio

The dashboard administration page edits the private runtime files and can
manage:

- Repeater identity and BrandMeister connection settings
- Static and bidirectional talkgroup mappings
- Dynamic talkgroup expiry
- Per-direction audio gain, AGC, and release timing
- LRRP polling and retry intervals

Start with one talkgroup and short test calls in both directions. Raise gains
one stage at a time and stop when speech peaks begin to sound rough. Back up
the runtime directory before broad changes.

The generated files are mode `0600`; the runtime and queue directories are
mode `0700`. Do not move them into the repository or attach them to an issue.

## 6. Configure APX Radios

Complete the CPS steps in [Motorola APX CPS Configuration](APX_CONFIGURATION.md).
At minimum, the active conventional personality must reference a conventional
FNE data profile with CAI Data Registration enabled and the same ARS server IP
as the bridge. Subscriber radio IDs are learned at registration time and do not
need to be added to source code.

After voice succeeds, test in this order:

1. APX registration and black-backed packet-data icon
2. Short TMS message between two locally registered APX radios
3. APX to BrandMeister and BrandMeister to APX messaging
4. Long TMS message reassembly
5. LRRP position polling and BrandMeister APRS forwarding

## 7. Updates and Backups

Before replacing an installation, stop the stateful services and preserve the
runtime directory:

```bash
sudo systemctl stop quantarbridge.service dvmhost.service
sudo tar -C /home/quantar -czf /root/quantar-runtime-backup.tar.gz quantar-runtime
sudo systemctl start dvmhost.service quantarbridge.service
```

The backup contains credentials and radio activity data. Store it privately.
The `--force` installer option replaces managed source and generated
configuration, so use it only after making and verifying this backup.

For detailed service operation and fault isolation, continue with
[Operation and Troubleshooting](OPERATIONS.md).
