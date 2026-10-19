from __future__ import annotations

import os
import re
import subprocess

import pytest

from tests.otcli import OtCli
from tools import fw

# What each board currently runs, so tests only rebuild/reflash when the image changes.
_flashed: dict[str, tuple] = {}


def ensure_flashed(target_name: str, **build_kwargs) -> None:
    target = fw.TARGETS[target_name]
    key = (target_name, tuple(sorted(build_kwargs.items())))
    if _flashed.get(target.board) != key:
        fw.build(target, pristine=True, **build_kwargs)
        fw.flash(target)
        _flashed[target.board] = key


def reset_board(name: str) -> None:
    fw.reset(fw.BOARDS[name])


P2_OUT = 0x50050404


def read_gpio_p2_out(name: str) -> int:
    """Read GPIO P2 OUT over SWD (the running CPU is not halted)."""
    board = fw.BOARDS[name]
    argv = fw._openocd(board, f"mdw {P2_OUT:#x}")
    out = subprocess.run(argv, capture_output=True, text=True, check=True, env={**os.environ, **fw.flash_env(board)})
    m = re.search(rf"{P2_OUT:#x}: ([0-9a-f]{{8}})", out.stdout + out.stderr)
    assert m, out.stdout + out.stderr
    return int(m.group(1), 16)


def form_network(cli: OtCli, name: str) -> str:
    """Form a new network on `cli`, wait until it leads, and return the active dataset as hex."""
    for c in ("dataset init new", f"dataset networkname {name}", "dataset commit active", "ifconfig up", "thread start"):
        cli.cmd(c)
    cli.wait_state({"leader"}, 30)
    return "".join(cli.cmd("dataset active -x"))


def _cli(board: str):
    cli = OtCli(fw.BOARDS[board].serial_port)
    cli.factory_reset()
    yield cli
    cli.close()


@pytest.fixture
def xiao():
    yield from _cli("xiao54")


@pytest.fixture
def dk():
    yield from _cli("dk")
