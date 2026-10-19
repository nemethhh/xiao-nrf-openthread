"""Join a board running apps/thread-cli to Home Assistant's preferred Thread network.

python -m tools.ha_join <board>             read HA's preferred dataset, write it to the board, start Thread, wait
python -m tools.ha_join <board> --router    ... and wait (up to 180 s) until the board is a router
python -m tools.ha_join <board> --dry-run   only print what it would do (reads HA's dataset list and the board's state)

Home Assistant is only read (thread/list_datasets, thread/get_dataset_tlv); the writes go to the board over its
serial shell: `ot dataset set active <tlv>`, `ot ifconfig up`, `ot thread start`. The board stores the dataset in
flash and rejoins the network on every boot by itself. The dataset TLV holds the network key: it is never printed,
and errors from the commands that carry it are reported without it.
"""
from __future__ import annotations

import argparse
import sys

from tests.otcli import OtCli, OtError
from tools import fw
from tools.ha import HaClient, HaError, Secret

ATTACHED = {"child", "router", "leader"}
JOINED = {"child", "router"}  # `leader` on HA's network would mean a partition of its own, not a join
JOIN_TIMEOUT = 90.0
ROUTER_TIMEOUT = 180.0


def preferred_dataset(ha) -> dict:
    preferred = [d for d in ha.thread_datasets() if d.get("preferred") is True]
    if len(preferred) != 1:
        raise RuntimeError(f"need exactly one preferred Thread dataset in Home Assistant, found {len(preferred)}")
    return preferred[0]


def describe(ds: dict) -> str:
    return (
        f"{ds.get('network_name')!r} (channel {ds.get('channel')}, PAN ID {ds.get('pan_id')}, "
        f"ext PAN ID {ds.get('extended_pan_id')}, source {ds.get('source')})"
    )


def _first(cli, command: str) -> str:
    out = cli.cmd(command)
    return out[0].strip() if out else ""


def on_network(cli, ds: dict) -> tuple[bool, str]:
    """(board is attached to `ds`'s network, board state). Compares name and extended PAN ID only:
    `dataset active` would print the network key."""
    state = _first(cli, "state")
    if state not in ATTACHED:
        return False, state
    same = _first(cli, "networkname") == ds.get("network_name") and (
        _first(cli, "extpanid").lower() == str(ds.get("extended_pan_id", "")).lower()
    )
    return same, state


def _set_dataset(cli, tlv: Secret) -> None:
    """The command carries the network key: keep it a Secret (redacted repr, so a traceback that prints
    OtCli.cmd's arguments does not show it) and report every failure without it or its cause."""
    try:
        cli.cmd("dataset set active " + Secret(tlv))
    except OtError as e:
        raise RuntimeError(f"`ot dataset set active` failed (error code {e.code})") from None
    except TimeoutError:
        raise RuntimeError("`ot dataset set active` timed out") from None
    except Exception as e:  # noqa: BLE001 - anything else may quote the command
        raise RuntimeError(f"`ot dataset set active` failed ({type(e).__name__})") from None


def run(board: str, ha, cli, *, dry_run: bool = False, router: bool = False, timeout: float = JOIN_TIMEOUT) -> int:
    ds = preferred_dataset(ha)
    print(f"Home Assistant preferred Thread network: {describe(ds)}")
    joined, state = on_network(cli, ds)
    print(f"{board}: state {state}")
    want = {"router"} if router else JOINED
    wait = ROUTER_TIMEOUT if router else timeout

    if joined:
        print(f"{board} is already on {ds.get('network_name')!r}: no dataset written")
        steps = [f"wait for state {'/'.join(sorted(want))} (up to {wait:g} s)"]
    else:
        steps = []
        if state != "disabled":
            steps += ["ot thread stop", "ot ifconfig down"]
        steps += [
            "ot dataset set active <HA preferred dataset TLV, not shown>",
            "ot ifconfig up",
            "ot thread start",
            f"wait for state {'/'.join(sorted(want))} (up to {wait:g} s)",
        ]
    print(("dry run (nothing is written); would run on " if dry_run else "on ") + f"{board}:")
    for s in steps:
        print(f" - {s}")
    if dry_run:
        return 0

    if not joined:
        if state != "disabled":
            cli.cmd("thread stop")
            cli.cmd("ifconfig down")
        _set_dataset(cli, ha.thread_dataset_tlv(ds["dataset_id"]))
        cli.cmd("ifconfig up")
        cli.cmd("thread start")

    if router:
        cli.wait_state(JOINED, timeout)
    state = cli.wait_state(want, wait)
    name = _first(cli, "networkname")
    if name != ds.get("network_name"):
        raise RuntimeError(f"board reports network name {name!r}, expected {ds.get('network_name')!r}")
    if _first(cli, "extpanid").lower() != str(ds.get("extended_pan_id", "")).lower():
        raise RuntimeError("board's extended PAN ID differs from Home Assistant's preferred dataset")
    mleid = _first(cli, "ipaddr mleid")
    print(f"{board}: {state} on {name!r}, rloc16 {_first(cli, 'rloc16')}, mleid {mleid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.ha_join", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("board", choices=fw.BOARDS)
    p.add_argument("--dry-run", action="store_true", help="print the plan only; nothing is written to the board")
    p.add_argument("--router", action="store_true", help=f"wait until the board is a router (up to {ROUTER_TIMEOUT:g} s)")
    p.add_argument("--timeout", type=float, default=JOIN_TIMEOUT, help="seconds to wait for child/router")
    a = p.parse_args(argv)
    try:
        with HaClient(token_path=str(fw.REPO / "ha_token")) as ha:
            cli = OtCli(fw.BOARDS[a.board].serial_port)
            try:
                return run(a.board, ha, cli, dry_run=a.dry_run, router=a.router, timeout=a.timeout)
            finally:
                cli.close()
    except (HaError, RuntimeError, TimeoutError, OtError) as e:
        print(f"ha_join: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ha_join: cannot reach Home Assistant or {a.board}: {type(e).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
