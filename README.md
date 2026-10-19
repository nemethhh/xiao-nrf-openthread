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

If the sniffer cannot be used, `rcp-xiao52` is the planned fallback RCP over USB CDC (built at the end of the RCP work).
