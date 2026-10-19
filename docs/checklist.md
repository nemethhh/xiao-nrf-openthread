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
