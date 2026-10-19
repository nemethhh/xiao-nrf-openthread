# xiao-nrf-openthread

Firmware and tooling for Seeed XIAO nRF boards on OpenThread with Home Assistant: a XIAO nRF54L15 is the RCP
radio of Home Assistant's OpenThread Border Router add-on, and a XIAO nRF52840 is a permanent Thread router
in that network, extending the mesh from any USB charger. (The repo was called `nrf54l15-thread-router`.)

| Board | Role |
| --- | --- |
| Seeed XIAO nRF54L15 | HA border router radio (RCP); earlier a standalone Thread router (CLI) |
| Seeed XIAO nRF52840 | permanent Thread router in HA's network (`router-xiao52`); re-flashable as 802.15.4 sniffer or fallback RCP |
| Nordic nRF54L15 DK | second Thread node for tests |

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
| `router-xiao52` | XIAO nRF52840 | Thread router CLI (`apps/thread-cli`), shell on native USB | UF2; see "XIAO nRF52840 Thread router" |
| `sniffer-xiao52` | XIAO nRF52840 | 802.15.4 sniffer | UF2 |
| `rcp-xiao52` | XIAO nRF52840 | fallback RCP over USB CDC ACM | UF2; untested on hardware |

UF2 flashing (XIAO nRF52840): `python -m tools.fw flash <target>` copies the image to the bootloader's drive (label
`XIAO-SENSE`, mounted with `udisksctl` if needed). While the board runs `router-xiao52`, the tool gets there by itself:
it writes the Adafruit bootloader's UF2 magic (`GPREGRET = 0x57`) through the shell's `devmem` and resets. Under any
other firmware (sniffer, RCP) double-tap the reset button first.

Flashing keeps the settings partition (a stored Thread network survives). `python -m tools.fw flash <target> --erase-settings`
also fills the settings partition with 0xff and reads it back first (SWD targets only; verified on the DK: the node comes back
`disabled` with no active dataset). If the read-back fails, OpenOCD stops before loading the image and leaves the CPU halted:
rerun the command, or `python -m tools.fw reset <board>`.

Test tiers:

```sh
.venv/bin/pytest -q                        # unit tests only (no hardware, no Home Assistant)
.venv/bin/pytest -m hw -v                  # boards on this PC: router, two-node, local RCP against an isolated OTBR
.venv/bin/pytest -m soak -v                # long RCP reliability soak
.venv/bin/pytest -m "ha and not hw" -v     # Home Assistant stage (read-only; skips until the XIAO is on the HA host)
HA_ALLOW_RESTART=1 .venv/bin/pytest -m "ha and hw" tests/ha/test_end_to_end.py -v   # end-to-end, flashes the DK
.venv/bin/pytest -m "ha and hw" tests/ha/test_xiao52_router.py -v   # nRF52840 router in HA's network (+ DK); re-provision after
```

`-m hw` router tests need the XIAO on this PC, so they are not rerun once it lives on Home Assistant.

## Home Assistant border router

Status: the HA stage is live and verified (2026-10-19): the XIAO runs `rcp-xiao54` and is the radio of HA's OpenThread Border Router add-on (`core_openthread_border_router` 3.3.0).
The XIAO was since reflashed on this PC with the RF-switch fix; until it is back on the HA host and re-verified, see
`docs/checklist.md`, "Antenna fix: reflash XIAO RCP".
The add-on must use `flow_control: true` (the XIAO's USB bridge is silent while DTR is deasserted). `tools/ha.py` is the
HA client (`python -m tools.ha info|serial|addon-info|datasets|find-xiao`, read-only CLI; `find-xiao` prints the XIAO's new by-id path on the HA host); `tools/ha_setup.py` installs/configures the
add-on, confirms discovery and marks the dataset preferred (`python -m tools.ha_setup` is a dry run, `--apply` writes).
`tests/ha/` checks the result and the end-to-end path (DK joins, gets an OMR address, pings the LAN via NAT64); tests
skip while the XIAO is not on the HA host. Step-by-step: `docs/checklist.md`, "Move XIAO to Home Assistant"
(DONE and verified live on 2026-10-19; only the add-on restart checks remain, see the checklist). The `ha_token` file (HA long-lived token) is never committed.

Fallback if the nRF54L15 RCP proves unreliable on HA: build and flash `rcp-xiao52` (USB CDC ACM) on the XIAO nRF52840.

## XIAO nRF52840 Thread router

The XIAO nRF52840 runs the same `apps/thread-cli` firmware as the nRF54L15 boards (FTD, OpenThread CLI in the Zephyr
shell). Its board file `apps/thread-cli/boards/xiao_ble_nrf52840.conf` puts the shell on the native USB port (CDC ACM)
and makes it headless: USB comes up by itself, OpenThread starts from `main()` when a dataset is stored, and the shell
holds its output until a host opens the port (DTR) instead of blocking. Thread settings live in ZMS on the 32 KiB
`storage_partition` at 0xec000 (internal flash, below the UF2 bootloader), so the network survives resets (tested)
and power loss (on flash, like a reset; the charger unplug check is in `docs/checklist.md`).

```sh
python -m tools.fw build router-xiao52
python -m tools.fw flash router-xiao52      # from the sniffer: double-tap reset first; from the router: automatic
#   port: /dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_Thread_Router_B46C2A1C09259A32-if00 (BOARDS["xiao52"])
python -m tools.ha_join xiao52 --dry-run    # what it would do
python -m tools.ha_join xiao52 --router     # join HA's preferred network and wait until it is a router
```

`tools/ha_join.py` reads Home Assistant's preferred Thread dataset (read-only: `thread/list_datasets`,
`thread/get_dataset_tlv`) and writes it into the board over its shell (`ot dataset set active <tlv>`, `ot ifconfig up`,
`ot thread start`, after `ot thread stop` if the board is on another network); a board already on the network is left
alone. It prints the plan first and never prints the TLV (it holds the network key). Any board in `fw.BOARDS` running
thread-cli works (the DK too). After that the board needs no host: plug it into any USB charger and it rejoins as a
router by itself. `tests/ha/test_xiao52_router.py` factory-resets the board before and after each test, so run
`python -m tools.ha_join xiao52 --router` again afterwards.

Back to the sniffer: `python -m tools.fw build sniffer-xiao52 && python -m tools.fw flash sniffer-xiao52` (automatic
bootloader entry from the router firmware); the port becomes `fw.XIAO52_SNIFFER_PORT`. The board then leaves HA's
mesh. Back to the router: double-tap reset, `python -m tools.fw flash router-xiao52`, then `ha_join` again.

## Sniffing

The XIAO nRF52840 is now a Thread router (see above); to sniff, re-flash it with `sniffer-xiao52`. Then it runs
NCS's `802154_sniffer` sample and shows up in Wireshark through Nordic's extcap.

```sh
# 1. Build (no overlay needed; xiao_ble already has a CDC ACM console) and flash.
python -m tools.fw build sniffer-xiao52
#    From the router firmware the flash enters the bootloader by itself; otherwise double-tap reset first.
python -m tools.fw flash sniffer-xiao52
#    The board re-enumerates as 1915:154b "nRF 802154 Sniffer" (by-id ...Nordic_Semiconductor_ASA_nRF_802154_Sniffer_...-if00).

# 2. Install the extcap once (needs python3 + pyserial; the system ones are enough).
mkdir -p ~/.local/lib/wireshark/extcap
cp docs/reference/nrf-sniffer-802154/nrf802154_sniffer/nrf802154_sniffer.py ~/.local/lib/wireshark/extcap/
chmod +x ~/.local/lib/wireshark/extcap/nrf802154_sniffer.py
#    tshark/wireshark need dumpcap access: sudo usermod -aG wireshark $USER, then log in again.

# 3. Capture 40 s on the network's channel (ask a node: `ot channel`, `ot panid`).
#    The interface name is the tty itself (`tshark -D` shows "/dev/ttyACM0 (nRF Sniffer for 802.15.4)"; the number can change).
#    The channel is the extcap pref `extcap.<sanitized if>.channel`: the interface name lowercased with non-alphanumerics
#    turned into "_", so /dev/ttyACM0 -> `_dev_ttyacm0`. (`extcap./dev/ttyACM0.channel` and `extcap.nrf802154_sniffer.channel`
#    are rejected as unknown preferences; `--channel` is not a tshark option.)
tshark -i /dev/ttyACM0 -o extcap._dev_ttyacm0.channel:14 -a duration:40 -w build/capture.pcapng
tshark -r build/capture.pcapng -Y 'wpan.dst_pan == 0xa667'       # PAN ID from `ot panid`; MLE advertisements show up
```

dumpcap needs group `wireshark` (`sudo usermod -aG wireshark $USER`, then log in again; until then
`printf '<command>\n' | newgrp wireshark` runs it with the group; `sg` may not be installed).
List the pref keys with `tshark -G currentprefs | grep extcap`.

Without dumpcap access the extcap can write a FIFO directly (untested):
`mkfifo /tmp/f; ~/.local/lib/wireshark/extcap/nrf802154_sniffer.py --capture --extcap-interface /dev/ttyACM0 --fifo /tmp/f --channel 14 &`
and `wireshark -k -i /tmp/f` (or `tshark -i /tmp/f`).

Decryption (UNTESTED, written from memory of Wireshark's prefs): give Wireshark the network key (`ot networkkey` on a node)
under Preferences > Protocols > Thread > "Thread Network Key", or under Protocols > IEEE 802.15.4 > Decryption Keys.
Frames are readable without the key (PAN ID, addresses, MLE structure).

`rcp-xiao52` is the fallback RCP over USB CDC if the XIAO nRF54L15 RCP misbehaves on Home Assistant.
