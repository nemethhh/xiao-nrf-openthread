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
