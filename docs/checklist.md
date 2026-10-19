# Manual checklist

Steps that need a human at the desk. Record results in `docs/hardware-notes.md`.

## Power-loss persistence (router)

Status: PENDING (needs a physical USB unplug).

1. Flash `router-xiao54`: `python -m tools.fw build router-xiao54 && python -m tools.fw flash router-xiao54`.
2. Open the serial port and form a network as `tests/hw/test_router.py::test_forms_network_and_becomes_leader` does
   (`ot dataset init new`, `ot dataset commit active`, `ot ifconfig up`, `ot thread start`), wait for `leader`.
3. Note the output of `ot dataset active -x`.
4. Unplug the XIAO's USB for 10 s, then replug.
5. Open the serial port and confirm `ot state` reaches `leader` on its own (no commands needed) and
   `ot dataset active -x` is identical to step 3.
6. Record pass/fail and the date in `docs/hardware-notes.md`.

The reset variant of this (SWD reset instead of power loss) is automated and passes.

## Sniffer

Status: PENDING (flashing needs a physical double-tap of reset; the live capture depends on it).

The XIAO nRF52840 (`2886:802f`, serial `B46C2A1C09259A32`) is on USB running an unknown Zephyr app. A 1200-baud
"touch" does not enter its bootloader, and there is no SWD probe.

1. `python -m tools.fw build sniffer-xiao52`
2. Double-tap the XIAO nRF52840's reset button; the UF2 drive mounts (`/run/media/$USER/XIAO*`; `udisksctl mount -b <dev>` if not).
3. `python -m tools.fw flash sniffer-xiao52`; the board re-enumerates as `1915:154b` "nRF 802154 Sniffer".
   Confirm `ls /dev/serial/by-id/` shows `usb-Nordic_Semiconductor_ASA_nRF_802154_Sniffer_B46C2A1C09259A32-if00`
   (the path in `BOARDS["xiao52"]`); fix it if it differs.
4. `sudo usermod -aG wireshark $USER` (dumpcap is `root:wireshark 0750`), log in again; `tshark -D` should list the nRF Sniffer.
5. Form a network on the XIAO nRF54L15 and join the DK (`pytest -m hw tests/hw/test_two_node.py` leaves them up, or use `form_network`),
   read `ot channel` / `ot panid`, then run the capture from README "Sniffing".
   Pass: `tshark -r build/capture.pcapng | wc -l` > 0 with the network's PAN ID.
6. Record the result in `docs/hardware-notes.md`.

## nRF52840 fallback RCP (optional)

Status: PENDING, only needed if the XIAO nRF54L15 RCP proves unreliable on Home Assistant (the local soak at 460800 passed).

`python -m tools.fw build rcp-xiao52` builds (`build/rcp-xiao52/rcp/zephyr/zephyr.uf2`, Spinel over USB CDC ACM). Flashing needs a double-tap of
reset on the XIAO nRF52840, then `python -m tools.fw flash rcp-xiao52`. Untested on hardware.

## Move XIAO to Home Assistant

Status: PENDING (needs the XIAO's USB cable moved by hand; completes Tasks 8-10).

The XIAO is flashed with `rcp-xiao54` at 460800 baud (soak passed 700/700 twice). Do not reflash it. The OTBR add-on
`core_openthread_border_router` 3.3.0 is already installed (stopped). Everything below is tooling that is already written
and unit-tested; the steps only need the board moved. `ha_token` must be in the repo root.

1. Unplug the XIAO from this PC and plug it into the Home Assistant host.
2. `.venv/bin/python -m tools.ha serial` — expect a new `...XIAO_nrf54_CMSIS-DAP_AA2C939D-if02` entry
   (baseline before plugging: only `/dev/ttyS0-3`, no by-id entries). If the path differs, set `XIAO_HA_DEVICE` in
   `tools/ha.py` (`find_new_xiao(baseline, current)` picks the single new `XIAO_nrf54` entry; zero or several = stop and
   investigate, do not guess). Record the confirmed path in `docs/hardware-notes.md`.
3. `.venv/bin/python -m tools.ha_setup` — dry run (read-only). Expect "device ... visible", options to be written
   (`flow_control: true`, baudrate 460800, nat64 and firewall on), then start.
4. `.venv/bin/python -m tools.ha_setup --apply` — writes: options, start, confirm otbr then thread discovery flows, wait for a
   dataset, mark it preferred (refuses with a message if a non-OTBR dataset is already preferred: ask which network should win).
   Safe to run twice.
5. `.venv/bin/pytest -m ha -v` — border-router tests (the restart test skips without `HA_ALLOW_RESTART=1`).
6. `HA_ALLOW_RESTART=1 .venv/bin/pytest -m "ha and hw" tests/ha/test_end_to_end.py -v` — DK joins HA's network, gets an OMR
   address, pings this PC's IPv4 via NAT64, and reattaches after an add-on restart. Setting `HA_ALLOW_RESTART=1` is the permission
   to restart the add-on. If the PC firewall drops ICMP, open it temporarily and close it afterwards.
7. Record results and the date in `docs/hardware-notes.md`.

If the add-on log (Settings > Add-ons > OpenThread Border Router > Log) shows Spinel / RCP init errors
(`Failed to initialize ... spinel`, `RCP is not responding`, timeouts after `otbr-agent` starts):

- The XIAO's SAMD11 USB bridge forwards nothing while DTR is deasserted. `flow_control: false` makes the add-on append
  `&uart-init-deassert` (drops DTR) and failed 8/8 locally; `flow_control: true` appends `&uart-flow-control` (CRTSCTS), which was
  harmless on the dev host's CDC-ACM but is unconfirmed on HA OS.
- First confirm the add-on options (`python -m tools.ha addon-info`) show `device = XIAO_HA_DEVICE`, baudrate 460800, flow_control true.
- If it still fails with true, try `flow_control: false` once (Settings > Add-ons > Configuration) and compare the log. Whichever
  works, set it in `otbr_options` (`tools/ha_setup.py`) and its unit test, and note it in `docs/hardware-notes.md`.
- Still failing: stop and use the sniffer (Sniffer section) plus the fallback `rcp-xiao52`.

Add-on restart permission: `restart_addon` is a Home Assistant write; tests do it only with `HA_ALLOW_RESTART=1`.
