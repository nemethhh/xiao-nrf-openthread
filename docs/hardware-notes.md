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
- `python -m tools.fw flash node-dk --erase-settings` verified on the DK (2026-10-19, later): after forming a network (leader),
  the flash fills the settings partition (0x174000, 0x9000) with 0xff, commits the RRAMC write-buffer and reads the range back
  before loading the image. The node comes back with `ot state` = `disabled` and `ot dataset active` = `Error 23: NotFound`, and
  again after a further `python -m tools.fw reset dk`. 7 cycles in all (4 with the plain fill, 3 with commit + read-back), all clean;
  ~18 s per flash. A dump of the partition after the erase shows only ZMS's fresh init (198 non-0xff bytes in sector 0).
- RRAMC (`0x5004B000`) behaviour measured over SWD on the DK: a full 128-bit line written by `mww` is committed at once;
  a partial line sits in the write-buffer (`BUFSTATUS.WRITEBUFEMPTY` 0) briefly, then commits (empty within 200 ms) and survives
  `reset halt`, even with `READYNEXTTIMEOUT` disabled (its reset value `0x80`; the running app sets `0x80000080`). So the
  uncommitted-write-buffer hypothesis for the DK's silent-CLI incident was not supported; the explicit `TASKS_COMMITWRITEBUF` is
  kept only as cheap insurance.
- A partial fill (simulating an interrupted erase) does leave inconsistent settings: erasing only the first 1 KiB of a formed
  network gave `disabled` with `dataset active` -> `Error 7: InvalidArgs`; erasing only sector 1 left the dataset and the node
  autostarted `detached`. Hence the read-back: a failed erase aborts OpenOCD before `nrf54l-load` (exit 1, CPU left halted).
  None of these reproduced the earlier incident (OT CLI silent after a reboot, shell alive); 4 form + `ot factoryreset` cycles
  were clean as well. Cause of that incident unknown (see the final-fix report).

## Standalone router, `apps/thread-cli` on the XIAO (2026-10-19)

- `tests/hw/test_router.py` passes 4/4: banner, leader formation, network survives SWD reset
  (rejoins with no commands, dataset unchanged), external-antenna build.
- Settings persistence: `CONFIG_SETTINGS=y` with ZMS in the settings partition (`storage_partition`, 36 KiB = 0x9000 at 0x174000 from the board DTS; Partition Manager is off, so
  `CONFIG_PM_PARTITION_SIZE_SETTINGS_STORAGE` has no effect and was removed from `apps/*/prj.conf`); stored network
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
  `/dev/serial/by-id/usb-Nordic_Semiconductor_ASA_nRF_802154_Sniffer_B46C2A1C09259A32-if00` (was `BOARDS["xiao52"]`, now `fw.XIAO52_SNIFFER_PORT`; this was a prediction, verified after flashing, see below).
- A 1200-baud open/close of the CDC ACM port did NOT enter the UF2 bootloader (the running app ignores it); the UF2 drive needs a double-tap of reset.
- Extcap (`nrf802154_sniffer.py` copied to `~/.local/lib/wireshark/extcap/`) runs on system python3 with system pyserial 3.5 (no venv needed);
  it matches the device by VID/PID 1915:154b. `--extcap-interfaces` works but lists no interface until the sniffer is flashed.
- `tshark -D` fails here with "Couldn't run dumpcap: Permission denied": `/usr/bin/dumpcap` is `root:wireshark 0750` and the user is not in group `wireshark`.

### Sniffer flashed and verified (2026-10-19, later)

- After the UF2 flash the board enumerates as `1915:154b` at exactly the predicted
  `/dev/serial/by-id/usb-Nordic_Semiconductor_ASA_nRF_802154_Sniffer_B46C2A1C09259A32-if00` (-> `/dev/ttyACM0`).
- `tshark -D` lists `/dev/ttyACM0 (nRF Sniffer for 802.15.4)` (run with group wireshark via newgrp; `sg` is not installed).
- Capture: DK alone as leader (channel 14, PAN `0xa667`), `tshark -i /dev/ttyACM0 -o extcap._dev_ttyacm0.channel:14 -a duration:40 -w build/capture.pcapng`
  -> 3 frames, all MLE advertisements (broadcast, ~10 s and ~13 s apart) with dst PAN 0xa667. The same command on channel 20 captured
  7 frames from another network (PAN 0xdb82, none with 0xa667), so the channel pref takes effect.
- Pref key: `extcap.<sanitized if>.channel` (`_dev_ttyacm0`); `extcap./dev/ttyACM0.channel` and `extcap.nrf802154_sniffer.channel` are unknown prefs.
- Decryption not tested. DK was factory-reset afterwards.

## XIAO nRF54L15 as RCP against an isolated local OTBR (2026-10-19)

- Firmware `apps/rcp` (NCS `openthread/coprocessor` sample): Spinel/HDLC on `uart20` (P1.8/P1.9, the SAMD11 bridge), no flow control,
  console/shell/log removed from the UART (`CONFIG_CONSOLE=n`, `CONFIG_LOG=n`, chosen `zephyr,console`/`shell-uart` deleted).
  Baud via `apps/rcp/overlays/baud-<N>.overlay`. 120 KB flash, 26 KB RAM.
- RCP version string: `OPENTHREAD/ncs-thread-reference-20250402-18-g0fe68ff23; Zephyr; Oct 19 2026 19:54:19`.
- OTBR: `openthread/border-router:sha-fd872ab@sha256:9b1bb839de08...` = ot-br-posix `fd872ab9d4af` (`otbr-agent` 0.3.0-fd872ab, Thread 1.4.0),
  run by `tools/otbr-local.sh` on the `--internal` network `otbr-isolated` (`docker network inspect ... -f '{{.Internal}}'` = `true`).
- Version check against HA add-on `openthread_border_router` 3.3.0 (build.yaml): stable OTBR `OTBR_VERSION: fd872ab9d4afdfaf034b3b1154fed08b8b6d1e43`
  (identical to the pinned image's revision label), beta `OTBR_BETA_VERSION: cb4bdae242b2...` (= Docker tag `v2026.10.0`). HA builds with its own
  config header and `OT_RCP_RESTORATION_MAX_COUNT=2`; the image is the upstream build, so behaviour is the same code, not the same binary.
- **HA's "flow control off" radio URL does not work with this bridge.** With flow control off the add-on appends `&uart-init-deassert`;
  otbr-agent then deasserts DTR/RTS and fails at start (`[C] Platform: Init() at spinel_driver.cpp:87: Failure`). Cause, measured with pyserial
  (Spinel `PROP_VALUE_GET NCP_VERSION` frame at 460800): the SAMD11 bridge forwards nothing while DTR is low:
  DTR=1/RTS=1 -> 92 bytes back, DTR=0/RTS=1 -> 0, DTR=1/RTS=0 -> 92, DTR=0/RTS=0 -> 0. (One early run with `uart-init-deassert` right after
  the first flash did work; every later run, 8 of 8 incl. after reflash and reset, failed. Not understood; do not rely on it.)
  Working URL forms at 460800: `?uart-baudrate=460800` and `?uart-baudrate=460800&uart-flow-control`. The latter is what the add-on emits with
  **"flow_control: true"**, and was observed harmless on the dev host and on HA OS (`flow_control: true` works there); the RCP UART
  has no flow control. (Why CRTSCTS does not stall the CMSIS-DAP CDC-ACM port was not investigated.) So for Home Assistant
  set `baudrate: 460800` and `flow_control: true` (even though there are no RTS/CTS wires). `tools/otbr-local.sh` selects the form with
  `OTBR_FLOW=hw|deassert|none`, default `hw`.
- Tests: `tests/hw/test_rcp_local.py` 3/3 pass at 460800 (RCP version, OTBR leader within 30 s, DK joins and pings 20/20).
- **Serial soak, 460800 baud, no hardware flow control, OTBR_FLOW=hw: PASS.** DK pings the OTBR mesh-local address: 600 x 64 B at 1 s
  + 100 x 1000 B at 0.5 s (fragmented) = 700/700 received (0.00% loss); container `RestartCount 0`, running; no RCP/Spinel
  reset/restore/fail/timeout lines in the OTBR log (672 s). Evidence: `build/soak-460800.txt`. No need to test 115200 or lower `RCP_BAUD`
  (`tools/fw.py` keeps `RCP_BAUD = 460800`). The soak is the single 12-minute run; longer-term stability is for Task 8 on HA.

### RF switch driven by the RCP image (2026-10-19, later)

- The first `rcp-xiao54` image (the one HA ran until now) had `CONFIG_GPIO=n` from the NCS sample, so neither the GPIO nor the
  fixed-regulator driver was built: the RF switch's power (rfsw-pwr, P2.3) and select (rfsw-ctl, P2.5) were not driven.
  Fixed in `apps/rcp/boards/xiao_nrf54l15_nrf54l15_cpuapp.conf` (commit 5d15ff5): `build/rcp-xiao54` `.config` has
  `CONFIG_GPIO=y`, `CONFIG_REGULATOR_FIXED=y`, and still no `CONFIG_CONSOLE`/`CONFIG_LOG`/`CONFIG_SHELL` (Spinel only on uart20).
- Fixed image flashed to the XIAO on this PC. Raw GPIO P2 over SWD: DIR = `0x128`,
  OUT = `0x108`: P2.3 is an output driven high (switch powered), P2.5 an output driven low (on-board ceramic antenna).
- `tests/hw/test_rcp_local.py` 5/5 pass at 460800, including the new `test_rcp_drives_rf_switch_to_internal_antenna` and the soak:
  700/700 received (600 x 64 B + 100 x 1000 B), container `0 true`, 1 otbr-agent start, 0 exits, no suspicious log lines
  (evidence `build/soak-460800.txt`, 21:31).
- Still to do with the XIAO back on the HA host: `docs/checklist.md`, "Antenna fix: reflash XIAO RCP".

## Home Assistant baseline (read-only, "before XIAO", 2026-10-19)
- HA at `http://192.168.50.10` (port 80), Core 2026.10.2; Supervisor calls work via WebSocket `supervisor/api` (`python -m tools.ha ...`).
- `python -m tools.ha serial`: **no serial devices with a `by_id` path** (empty list). Hardware info has 77 devices; the only tty nodes are
  `/dev/ttyS0`..`ttyS3` (onboard UARTs, no by-id).
- Add-on `core_openthread_border_router` 3.3.0 (latest 3.3.0) is **already installed**, state `stopped`, boot `auto`; options currently:
  `device: null`, `baudrate: "460800"`, `flow_control: true`, `otbr_log_level: notice`, `firewall: true`, `nat64: false`, `beta: false`.
- `thread/list_datasets` -> `unknown_command` (thread integration not loaded).

## Home Assistant border router (2026-10-19)
- The XIAO nRF54L15 (RCP firmware, 460800 baud) now sits on the HA host. `python -m tools.ha find-xiao` found exactly one new device:
  `/dev/serial/by-id/usb-Seeed_Studio_Seeed_Studio_XIAO_nrf54_CMSIS-DAP_AA2C939D-if02`, identical to the predicted `XIAO_HA_DEVICE`.
- The user ran `.venv/bin/python -m tools.ha_setup --apply` from their own terminal (the agent's permission classifier blocks this HA write):
  "OTBR set up: add-on started, flows confirmed, preferred dataset set". Options: baudrate "460800", flow_control true, nat64 true,
  firewall true, otbr_log_level notice. `flow_control: true` works on HA OS (confirms the DTR ruling on the HA kernel).
- Add-on `core_openthread_border_router` 3.3.0: state started. Preferred dataset: network `ha-thread-a98e`, channel 15, source otbr.
- `pytest -m "ha and not hw" -v`: test_xiao_visible_on_ha, test_otbr_addon_running_with_xiao, test_thread_dataset_preferred PASSED;
  test_otbr_survives_addon_restart SKIPPED (needs HA_ALLOW_RESTART=1).
- `pytest -m "ha and hw" tests/ha/test_end_to_end.py -v`: test_dk_joins_ha_network, test_dk_has_omr_address,
  test_dk_pings_lan_ipv4_via_nat64 PASSED (45 s); test_dk_reattaches_after_addon_restart SKIPPED (needs HA_ALLOW_RESTART=1).
- Sniffer verified separately (see commit b070e30). Restart checks remain: `HA_ALLOW_RESTART=1 .venv/bin/pytest -m ha -v` (user runs it).

## XIAO nRF52840 as a Thread router in Home Assistant's network (2026-10-19)

- Firmware `router-xiao52` = `apps/thread-cli` for `xiao_ble` (FTD) with `apps/thread-cli/boards/xiao_ble_nrf52840.conf`:
  USB device_next CDC ACM console/shell, `CDC_ACM_SERIAL_INITIALIZE_AT_BOOT`, `SHELL_BACKEND_SERIAL_CHECK_DTR` (shell output
  held until a host asserts DTR; printk drops when the CDC ring is full, nothing blocks), no `CONFIG_LOG`. Flash 495040 B of
  788 KB (61%), RAM 101496 B of 256 KB (39%). UF2 990208 B at 0x27000 (Adafruit bootloader 0.6.1, S140 7.3.0, board ID
  `Seeed_XIAO_nRF52840_Sense`, drive label `XIAO-SENSE`).
- USB identity 2fe3:0004 (Zephyr default VID/PID), manufacturer "Seeed Studio", product "XIAO nRF52840 Thread Router",
  serial `B46C2A1C09259A32`: **by-id `/dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_Thread_Router_B46C2A1C09259A32-if00`**
  (`BOARDS["xiao52"]`; the sniffer's port is kept as `fw.XIAO52_SNIFFER_PORT`).
- Settings: `CONFIG_SETTINGS_ZMS` on `storage_partition` = `partition@ec000`, `reg = <0xec000 0x8000>` (32 KiB, 8 x 4 KiB
  sectors, `SETTINGS_ZMS_SECTOR_SIZE_MULT=1`) from `nrf52840_partition_uf2_sdv7.dtsi`, between the app and the UF2
  bootloader at 0xf4000. ZMS works on the nRF52840's page-erase flash; the stored network survives `ot reset` (tested).
- Shell RX over USB: the default 64-byte `SHELL_BACKEND_SERIAL_RX_RING_BUFFER_SIZE` dropped everything after 64 characters of
  a pasted line (a 236-character `ot extpanid ...` echoed only 64; `ot dataset set active <tlv>` got neither Done nor an
  error). 512 bytes fixes it (whole line echoed, `Error 7: InvalidArgs` as expected).
- Opening the port releases the output held back until DTR (boot banner, prompt); the host tty echoes it into the shell
  before pyserial switches to raw mode, so the first command arrives as `*** Booting nRF .ot state` ->
  `***: command not found`. `OtCli` sends an empty line on every (re)open.
- A reset re-enumerates USB (the tty disappears): `OtCli` reopens the port (`PortLost`, a TimeoutError, then reopen).
- UF2 entry without a double-tap: `devmem 0x4000051c 32 0x57` (POWER GPREGRET = the Adafruit bootloader's UF2 magic) then
  `devmem 0xe000ed0c 32 0x5fa0004` (SCB AIRCR SYSRESETREQ) -> drive `XIAO-SENSE` appears, `tools.fw flash` mounts it and
  copies the image (verified twice). The first try without the read-back failed: the GPREGRET write was the swallowed
  first line. The UF2 drive vanishes as soon as the image is written: `shutil.copy`'s chmod then raised FileNotFoundError
  (the flash itself had worked); `shutil.copyfile` is used now.
- `tests/ha/test_xiao52_router.py` (ha + hw), runs: 5/5, 4/5 (see the ping note below), then
  5/5 and 5/5 back to back after the fixture change:
  joins `ha-thread-a98e` (networkname and extpanid match, as child within seconds), becomes router in 14-45 s with
  4-6 router-table rows (OTBR 0x2400 with LQ 3/3, itself, stale IDs of earlier test runs), back as child 2-3 s after
  `ot reset` with no commands (same network, same mleid, port re-enumerated), DK joins HA's network and pings the
  nRF52840's mleid 10/10, and with the nRF52840's port closed after a reset (no host reading, DTR low) the DK reaches its
  mleid at the first try, 10 s after the reset.
  One earlier run pinged 7/10 while the nRF52840 was still a child about to upgrade to router (its RLOC16 changes); the DK
  tests now start once it is a router.
- `tests/ha/test_end_to_end.py` (DK via HA, OMR, NAT64) still 3/3 with the OtCli changes (restart test skipped).
- Provisioned permanently with `python -m tools.ha_join xiao52 --router`: router on `ha-thread-a98e`, channel 15, rloc16
  0xd000. After a further `ot reset` it was a router again within 2 s. The DK was left factory-reset (`disabled` after an
  SWD reset).
- Power loss on a USB charger (no host at all): PENDING, `docs/checklist.md`, "XIAO nRF52840 router: power loss on a USB charger".
