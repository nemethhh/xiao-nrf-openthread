"""Set up the OpenThread Border Router add-on on Home Assistant with the XIAO RCP.

python -m tools.ha_setup          dry run (read-only): print what --apply would do
python -m tools.ha_setup --apply  perform the writes (install if missing, options, start, confirm flows, preferred)
"""
from __future__ import annotations

import sys
import time
from typing import Callable

from tools.fw import RCP_BAUD
from tools.ha import HA_SERIAL_BASELINE, OTBR_SLUG, XIAO_HA_DEVICE, HaClient, HaError, find_new_xiao

OTBR_SOURCE_MARKERS = ("otbr", "openthread", "open thread", "border router")


def otbr_options(device: str, baud: int) -> dict:
    """Add-on options per add-on 3.3.0 schema. flow_control must be True: the XIAO's USB bridge forwards
    nothing while DTR is deasserted, and flow_control False makes the add-on drop DTR (Task 6 ruling).
    The 3.3.0 schema has no autoflash_firmware key, so none is sent."""
    return {
        "device": device,
        "baudrate": str(baud),
        "flow_control": True,
        "nat64": True,
        "firewall": True,
        "otbr_log_level": "notice",
    }


def _is_otbr_source(ds: dict) -> bool:
    src = str(ds.get("source", "")).lower()
    return any(m in src for m in OTBR_SOURCE_MARKERS)


def _wait_for(pred: Callable[[], bool], timeout: float, sleep: Callable[[float], None], what: str) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout:g}s waiting for {what}")
        sleep(2.0)


def _write(action: Callable[[], object], settled: Callable[[], bool], timeout: float,
           sleep: Callable[[float], None], what: str) -> None:
    """Run a write once. If HA's reply times out, re-check the resulting state instead of retrying."""
    try:
        action()
    except HaError as e:
        if e.code != "timeout":
            raise
        _wait_for(settled, timeout, sleep, f"{what} to take effect after a reply timeout")


def _options_match(info: dict | None, want: dict) -> bool:
    have = (info or {}).get("options") or {}
    return all(have.get(k) == v for k, v in want.items())


def _check_device(client, device: str) -> None:
    if device not in client.serial_devices():
        raise RuntimeError(f"{device} is not visible to Home Assistant; plug the XIAO in and re-check `python -m tools.ha serial`")


def check_xiao_path(client) -> None:
    """The single new XIAO entry (vs the recorded baseline) must equal XIAO_HA_DEVICE."""
    found = find_new_xiao(HA_SERIAL_BASELINE, client.serial_devices())  # ValueError on 0 or several
    if found != XIAO_HA_DEVICE:
        raise RuntimeError(f"XIAO_HA_DEVICE ({XIAO_HA_DEVICE}) differs from the device found on HA ({found}); update XIAO_HA_DEVICE in tools/ha.py")


def plan_otbr(client, *, device: str, baud: int) -> list[str]:
    """Read-only description of what setup_otbr would do right now."""
    want = otbr_options(device, baud)
    info = client.addon_info(OTBR_SLUG)
    lines = []
    visible = device in client.serial_devices()
    lines.append(f"device {device}: {'visible' if visible else 'NOT visible to HA (--apply will refuse)'}")
    if info is None:
        lines.append(f"install add-on {OTBR_SLUG}")
        info = {}
    else:
        lines.append(f"add-on {OTBR_SLUG} already installed, state {info.get('state')}: no install")
    if _options_match(info, want):
        lines.append("options already match: no write")
    else:
        lines.append(f"set options {want} (current device={((info.get('options') or {}).get('device'))!r})")
    if info.get("state") == "started":
        lines.append("add-on already started: no start")
    else:
        lines.append("start add-on and wait for state started")
    for h in ("otbr", "thread"):
        n = len(client.discovery_flows(h))
        lines.append(f"confirm {n} pending {h} discovery flow(s)")
    try:
        ds = client.thread_datasets()
    except HaError as e:
        lines.append(f"thread datasets not readable yet ({e.code}); will wait for a dataset after confirming flows")
    else:
        pref = [d for d in ds if d.get("preferred")]
        lines.append(f"{len(ds)} dataset(s) now, preferred: {len(pref)}; mark the OTBR dataset preferred only if none is preferred")
    return lines


def setup_otbr(client, *, device: str, baud: int, timeout: float = 180.0,
               sleep: Callable[[float], None] = time.sleep) -> None:
    """Idempotent. Safe to run twice."""
    _check_device(client, device)
    want = otbr_options(device, baud)

    if client.addon_info(OTBR_SLUG) is None:
        _write(lambda: client.install_addon(OTBR_SLUG), lambda: client.addon_info(OTBR_SLUG) is not None,
               timeout, sleep, "install")
        if client.addon_info(OTBR_SLUG) is None:
            raise RuntimeError("add-on still not installed after install")

    options_written = False
    if not _options_match(client.addon_info(OTBR_SLUG), want):
        options_written = True
        _write(lambda: client.set_addon_options(OTBR_SLUG, want),
               lambda: _options_match(client.addon_info(OTBR_SLUG), want), timeout, sleep, "options")

    def started() -> bool:
        return (client.addon_info(OTBR_SLUG) or {}).get("state") == "started"

    if not started():
        _write(lambda: client.start_addon(OTBR_SLUG), started, timeout, sleep, "start")
        _wait_for(started, timeout, sleep, "add-on state started")
    elif options_written:
        # Already running with the old configuration: restart so the new options take effect.
        _write(lambda: client.restart_addon(OTBR_SLUG), started, timeout, sleep, "restart")
        _wait_for(started, timeout, sleep, "add-on state started after restart")

    def confirm_pending_and_check() -> bool:
        for handler in ("otbr", "thread"):
            for flow in client.discovery_flows(handler):
                try:
                    client.confirm_flow(flow["flow_id"])
                except HaError as e:
                    if e.code not in ("http_404", "http_400"):  # flow vanished between list and confirm
                        raise
        try:
            return any(_is_otbr_source(d) for d in client.thread_datasets())
        except HaError as e:
            if e.code == "unknown_command":  # thread integration not loaded yet
                return False
            raise

    _wait_for(confirm_pending_and_check, timeout, sleep, "an OTBR Thread dataset")

    datasets = client.thread_datasets()
    preferred = [d for d in datasets if d.get("preferred")]
    if preferred:
        if all(_is_otbr_source(d) for d in preferred) and len(preferred) == 1:
            return  # already done by a previous run
        raise RuntimeError(
            "a non-OTBR Thread dataset is already preferred; not changing it. Ask the user which network should be preferred."
        )
    otbr = [d for d in datasets if _is_otbr_source(d)]
    if len(otbr) != 1:
        raise RuntimeError(f"expected exactly one OTBR Thread dataset, found {len(otbr)}; not guessing")
    client.set_preferred_dataset(otbr[0]["dataset_id"])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in argv
    if set(argv) - {"--apply"}:
        print("usage: python -m tools.ha_setup [--apply]", file=sys.stderr)
        return 2
    try:
        with HaClient() as ha:
            if apply:
                check_xiao_path(ha)
                setup_otbr(ha, device=XIAO_HA_DEVICE, baud=RCP_BAUD)
                print("OTBR set up: add-on started, flows confirmed, preferred dataset set")
                return 0
            print("dry run (read-only); pass --apply to perform the writes")
            lines = plan_otbr(ha, device=XIAO_HA_DEVICE, baud=RCP_BAUD)
            for line in lines:
                print(" -", line)
            return 1 if any("NOT visible" in x for x in lines) else 0
    except (HaError, RuntimeError, TimeoutError, ValueError) as e:
        print(f"ha_setup: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"cannot reach Home Assistant: {type(e).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
