# nrf54l15-thread-router

Firmware and tooling that turn a Seeed XIAO nRF54L15 into a Thread router and, in the end
state, the RCP radio of Home Assistant's OpenThread Border Router add-on.

| Board | Role |
| --- | --- |
| Seeed XIAO nRF54L15 | standalone Thread router (CLI), then HA border router radio (RCP) |
| Nordic nRF54L15 DK | second Thread node for tests |
| Seeed XIAO nRF52840 | 802.15.4 sniffer; fallback RCP over native USB |

## Setup (Arch / CachyOS)

```sh
sudo pacman -S --needed openocd cmake ninja dtc wget python-pip wireshark-qt
# udev: /etc/udev/rules.d/99-thread-router.rules gives group uucp access to
#   2886:0066 (XIAO CMSIS-DAP, usb + hidraw) and 1366:* (SEGGER J-Link)

# nRF Connect SDK, version pinned in ./ncs-version
wget -O ~/.local/bin/nrfutil https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/x86_64-unknown-linux-gnu/nrfutil
chmod +x ~/.local/bin/nrfutil
nrfutil install sdk-manager
nrfutil sdk-manager install $(cat ncs-version)     # SDK lands in ~/ncs/<version>

python -m venv .venv && .venv/bin/pip install pytest pyserial websockets   # = [dev] extras in pyproject.toml
```

Reference sources (not committed) live in `docs/reference/`, cloned with `gh repo clone ... -- --depth 1`:
`openthread/openthread`, `Seeed-Studio/platform-seeedboards`, `home-assistant/addons`,
`openthread/ot-br-posix`, `NordicSemiconductor/nRF-Sniffer-for-802.15.4`.

Board targets (NCS v3.4.1): `xiao_nrf54l15/nrf54l15/cpuapp`, `nrf54l15dk/nrf54l15/cpuapp`,
`xiao_ble` (XIAO nRF52840). This repo is a freestanding app repo: builds run inside the NCS
toolchain with `ZEPHYR_BASE=~/ncs/<version>/zephyr` set (`tools/fw.py` does this).

## Build, flash, test

`python -m tools.fw build|flash|reset|recover <target>` wraps the NCS toolchain (never call `west` directly).

| Target | Board | What | Notes |
| --- | --- | --- | --- |
| `router-xiao54` | XIAO nRF54L15 | Thread router CLI (`apps/thread-cli`) | SWD flash via OpenOCD (CMSIS-DAP) |
| `node-dk` | nRF54L15 DK | Thread test node CLI | J-Link: `OPENOCD_INTERFACE=jlink` is set by `tools/fw.py` |
| `rcp-xiao54` | XIAO nRF54L15 | OpenThread RCP at 460800 baud (`apps/rcp`) | the border-router radio; the image HA uses |
| `sniffer-xiao52` | XIAO nRF52840 | 802.15.4 sniffer | UF2 flash after reset double-tap |
| `rcp-xiao52` | XIAO nRF52840 | fallback RCP over USB CDC ACM | UF2; untested on hardware |

Test tiers:

```sh
.venv/bin/pytest -q                        # unit tests only (no hardware, no Home Assistant)
.venv/bin/pytest -m hw -v                  # boards on this PC: router, two-node, local RCP against an isolated OTBR
.venv/bin/pytest -m soak -v                # long RCP reliability soak
.venv/bin/pytest -m "ha and not hw" -v     # Home Assistant stage (read-only; skips until the XIAO is on the HA host)
HA_ALLOW_RESTART=1 .venv/bin/pytest -m "ha and hw" tests/ha/test_end_to_end.py -v   # end-to-end, flashes the DK
```

`-m hw` router tests need the XIAO on this PC, so they are not rerun once it lives on Home Assistant.

## Home Assistant border router

The XIAO runs `rcp-xiao54` and is the radio of HA's OpenThread Border Router add-on (`core_openthread_border_router` 3.3.0).
The add-on must use `flow_control: true` (the XIAO's USB bridge is silent while DTR is deasserted). `tools/ha.py` is the
HA client (`python -m tools.ha info|serial|addon-info|datasets`, read-only CLI); `tools/ha_setup.py` installs/configures the
add-on, confirms discovery and marks the dataset preferred (`python -m tools.ha_setup` is a dry run, `--apply` writes).
`tests/ha/` checks the result and the end-to-end path (DK joins, gets an OMR address, pings the LAN via NAT64); tests
skip while the XIAO is not on the HA host. Step-by-step: `docs/checklist.md`, "Move XIAO to Home Assistant"
(status PENDING until the cable is moved). The `ha_token` file (HA long-lived token) is never committed.

Fallback if the nRF54L15 RCP proves unreliable on HA: build and flash `rcp-xiao52` (USB CDC ACM) on the XIAO nRF52840.

## Sniffing

The XIAO nRF52840 runs NCS's `802154_sniffer` sample and shows up in Wireshark through Nordic's extcap.

```sh
# 1. Build (no overlay needed; xiao_ble already has a CDC ACM console) and flash.
python -m tools.fw build sniffer-xiao52
#    Double-tap the reset button so the UF2 drive (XIAO...) mounts under /run/media/$USER/, then:
python -m tools.fw flash sniffer-xiao52
#    The board re-enumerates as 1915:154b "nRF 802154 Sniffer" (by-id ...Nordic_Semiconductor_ASA_nRF_802154_Sniffer_...-if00).

# 2. Install the extcap once (needs python3 + pyserial; the system ones are enough).
mkdir -p ~/.local/lib/wireshark/extcap
cp docs/reference/nrf-sniffer-802154/nrf802154_sniffer/nrf802154_sniffer.py ~/.local/lib/wireshark/extcap/
chmod +x ~/.local/lib/wireshark/extcap/nrf802154_sniffer.py
#    tshark/wireshark need dumpcap access: sudo usermod -aG wireshark $USER, then log in again.

# 3. Capture 30 s on the network's channel (ask the router: `ot channel`, `ot panid`).
IF=$(tshark -D | awk '/nRF Sniffer for 802.15.4/ {print $2}')    # e.g. /dev/ttyACM0-None
tshark -i "$IF" -o "extcap.$IF.channel:15" -a duration:30 -w build/capture.pcapng
tshark -r build/capture.pcapng | wc -l                           # > 0, PAN ID matches `ot panid`
```

Without dumpcap access the extcap can write a FIFO directly:
`mkfifo /tmp/f; ~/.local/lib/wireshark/extcap/nrf802154_sniffer.py --capture --extcap-interface <tty> --fifo /tmp/f --channel 15 &`
and `wireshark -k -i /tmp/f` (or `tshark -i /tmp/f`).

To decrypt Thread traffic, give Wireshark the network key (`ot networkkey` on a node) under
Preferences > Protocols > Thread > "Thread Network Key" (it derives the MLE/MAC keys itself; set the
sequence counter if prompted), or add it under Protocols > IEEE 802.15.4 > Decryption Keys.
Frames are still readable without the key (PAN ID, addresses, MLE structure).

`rcp-xiao52` is the fallback RCP over USB CDC if the XIAO nRF54L15 RCP misbehaves on Home Assistant.
