from __future__ import annotations

import os
import re
import subprocess
import warnings

import pytest

from tests.otcli import OtCli
from tools import fw

def pytest_configure(config) -> None:
    """Refuse `-l/--showlocals`: frame locals can hold Home Assistant's Thread network key (the shell echoes
    `ot dataset set active <tlv>` back into OtCli.cmd's locals). Secret (tools/ha.py) only covers repr() of values
    that keep its type, which is all a default long traceback prints (function arguments)."""
    if config.getoption("showlocals", False):
        raise pytest.UsageError("--showlocals/-l is refused in this repo: test locals can hold the Thread network key")


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
P2_IN = 0x50050410
P2_DIR = 0x50050414


def read_gpio_p2(name: str, reg: int = P2_OUT) -> int:
    """Read a GPIO P2 register over SWD (the running CPU is not halted)."""
    board = fw.BOARDS[name]
    argv = fw._openocd(board, f"mdw {reg:#x}")
    out = subprocess.run(argv, capture_output=True, text=True, check=True, env={**os.environ, **fw.flash_env(board)})
    m = re.search(rf"{reg:#x}: ([0-9a-f]{{8}})", out.stdout + out.stderr)
    assert m, out.stdout + out.stderr
    return int(m.group(1), 16)


def read_gpio_p2_out(name: str) -> int:
    return read_gpio_p2(name, P2_OUT)


RFSW_PWR_BIT = 1 << 3  # P2.3, active high: powers the XIAO's RF switch
RFSW_CTL_BIT = 1 << 5  # P2.5, active low: low selects the internal ceramic antenna, high the U.FL


def assert_antenna_pin(external: bool, name: str = "xiao54") -> None:
    """The XIAO's RF switch must be powered (P2.3) and set (P2.5), both driven as outputs.

    OUT alone is not proof (low is the reset value), so DIR must have both bits set as well.
    """
    direction, out = read_gpio_p2(name, P2_DIR), read_gpio_p2_out(name)
    regs = f"P2 OUT={out:#x} DIR={direction:#x}"
    assert direction & RFSW_PWR_BIT, f"P2.3 is not an output: RF switch not powered ({regs})"
    assert out & RFSW_PWR_BIT, f"P2.3 is low: RF switch not powered ({regs})"
    assert direction & RFSW_CTL_BIT, f"P2.5 is not an output: RF switch not driven ({regs})"
    high = bool(out & RFSW_CTL_BIT)
    assert high == external, (
        f"P2.5 is {'high' if high else 'low'} but the build is {'external' if external else 'internal'} ({regs})"
    )


def form_network(cli: OtCli, name: str) -> str:
    """Form a new network on `cli`, wait until it leads, and return the active dataset as hex."""
    for c in ("dataset init new", f"dataset networkname {name}", "dataset commit active", "ifconfig up", "thread start"):
        cli.cmd(c)
    cli.wait_state({"leader"}, 30)
    return "".join(cli.cmd("dataset active -x"))


def _cli(board: str):
    """Factory-reset before and after the test, so no network (e.g. Home Assistant's key) stays in flash.

    The reset afterwards is best effort: a failure there only warns, never hiding the test's own result.
    """
    cli = OtCli(fw.BOARDS[board].serial_port)
    try:
        cli.factory_reset()
        yield cli
        try:
            cli.factory_reset()
        except Exception as e:  # noqa: BLE001 - teardown must not mask the test outcome
            warnings.warn(f"{board}: factory reset after the test failed: {type(e).__name__}: {e}", stacklevel=1)
    finally:
        cli.close()


@pytest.fixture
def xiao():
    yield from _cli("xiao54")


@pytest.fixture
def dk():
    yield from _cli("dk")
