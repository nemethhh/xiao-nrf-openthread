# Hardware notes

Measured facts about the boards and links. Newest entries at the bottom of each section.

## Flashing (2026-10-19)

- Both nRF54L15 boards flash with system OpenOCD 0.12 using the XIAO board's
  `~/ncs/v3.4.1/zephyr/boards/seeed/xiao_nrf54l15/support/openocd.cfg` (`nrf54l-load`):
  - XIAO: CMSIS-DAP (SAMD11 bridge) serial `AA2C939D`, ~59 KiB/s.
  - DK: same config with `OPENOCD_INTERFACE=jlink`, J-Link OB serial `001057713759`.
    No SEGGER software or nrfutil-device needed.
- The same config provides `nrf54l_mass_erase` (CTRL-AP ERASEALL) and auto-recovers an
  AP-locked chip on examine failure; `python -m tools.fw recover <board>` calls it.
- Blinky verified on both boards by sampling GPIO P2 OUT (`0x50050404`) over SWD:
  XIAO bit 0 (P2.0, LED0) and DK bit 9 (P2.9, LED0) toggle.

## Standalone router, `apps/thread-cli` on the XIAO (2026-10-19)

- `tests/hw/test_router.py` passes 4/4: banner, leader formation, network survives SWD reset
  (rejoins with no commands, dataset unchanged), external-antenna build.
- Settings persistence: `CONFIG_SETTINGS=y` with ZMS in the settings partition (32 KiB); stored network
  survives reflash and reset. A node with a stored network autostarts, which swallows a lone
  `ot factoryreset`, so `OtCli.factory_reset` re-sends until the node reports `disabled`.
- Antenna switch measured electrically (GPIO P2 OUT `0x50050404` over SWD; RF switch control is P2.5, bit 5):
  - internal build: P2 OUT = `0x108`, P2 DIR = `0x128`: bit 5 is an output and low (U.FL not selected).
  - external build: P2 OUT = `0x128`, P2 DIR = `0x128`: bit 5 is an output and high (U.FL selected).
  - OUT alone is not proof (low is the reset value), so the tests also assert DIR bit 5 (0x50050414) is set.
  This backs the "never silently the wrong antenna" anti-goal: the banner and the pin agree for both builds.
- Power-loss persistence (USB unplug 10 s): PENDING, needs a human; see `docs/checklist.md`.

## DK as second Thread node (2026-10-19)

- DK console (Zephyr shell, `uart:~$`) is the J-Link VCOM `-if02`; `-if00` is silent. `BOARDS["dk"].serial_port` is `-if02`.
- The J-Link OB drops memory writes during `nrf54l-load` at the 1000 kHz adapter speed set by the board config
  (`Error: Failed to write memory at 0x...`, at a different address each run, even after a mass erase).
  `adapter speed 500` before `init` makes the 482 KiB image flash reliably (~16 s); applied to J-Link boards only in `tools/fw.py`.
- `ot ping` output on the DK: `N packets transmitted, M packets received.` (+ loss/RTT lines); 20/20 received, RTT 13-27 ms.
  The shell redraws `uart:~$ ` inside asynchronous output (even glued to `Done`), so `OtCli` strips the prompt anywhere in a line.
- `tests/hw/test_two_node.py` passes 3/3 (join + ping 20/20, DK upgrades to router with 2 router-table rows, DK reattaches after XIAO reset).

## XIAO nRF52840 sniffer (2026-10-19)

- Board on USB: `2886:802f` ("ZEPHYR" / "USB-DEV", serial `B46C2A1C09259A32`), running an unknown Zephyr app.
  `2886:802f` is what `xiao_ble` uses by default (Zephyr CDC ACM defaults); no SWD probe is attached.
  Current ports: `/dev/serial/by-id/usb-ZEPHYR_USB-DEV_B46C2A1C09259A32-if00` and `-if02`.
- `python -m tools.fw build sniffer-xiao52` works with no overlay: `xiao_ble` (resolves to `xiao_ble/nrf52840`) already
  defines `cdc_acm_uart0` through `boards/common/usb/cdc_acm_serial.dtsi`, as the nRF52840 Dongle does.
  Output `build/sniffer-xiao52/802154_sniffer/zephyr/zephyr.uf2` (258048 B, start 0x27000, matches the XIAO partition table
  for the Adafruit bootloader), flash 16% / RAM 22%.
- Sniffer firmware USB identity (from its `prj.conf`): VID `1915`, PID `154b`, manufacturer "Nordic Semiconductor ASA",
  product "nRF 802154 Sniffer", serial = HWINFO device id (same `B46C2A1C09259A32`). Expected port after flashing:
  `/dev/serial/by-id/usb-Nordic_Semiconductor_ASA_nRF_802154_Sniffer_B46C2A1C09259A32-if00` (in `BOARDS["xiao52"]`; prediction, unverified).
- A 1200-baud open/close of the CDC ACM port did NOT enter the UF2 bootloader (the running app ignores it); the UF2 drive needs a double-tap of reset.
- Extcap (`nrf802154_sniffer.py` copied to `~/.local/lib/wireshark/extcap/`) runs on system python3 with system pyserial 3.5 (no venv needed);
  it matches the device by VID/PID 1915:154b. `--extcap-interfaces` works but lists no interface until the sniffer is flashed.
- `tshark -D` fails here with "Couldn't run dumpcap: Permission denied": `/usr/bin/dumpcap` is `root:wireshark 0750` and the user is not in group `wireshark`.
