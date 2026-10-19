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
