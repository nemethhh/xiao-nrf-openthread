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
