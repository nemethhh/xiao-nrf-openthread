"""Goal 2: the nRF54L15 DK joins the network formed by the XIAO."""

import re

import pytest

from tests.conftest import ensure_flashed, form_network, reset_board
from tests.otcli import OtCli  # noqa: F401  (type of the fixtures)

pytestmark = pytest.mark.hw


@pytest.fixture
def images():
    ensure_flashed("router-xiao54")
    ensure_flashed("node-dk")


@pytest.fixture
def xiao(images, xiao):
    return xiao


@pytest.fixture
def dk(images, dk):
    return dk


def join(xiao, dk) -> str:
    dataset = form_network(xiao, "xiao-two")
    for c in (f"dataset set active {dataset}", "ifconfig up", "thread start"):
        dk.cmd(c)
    return dk.wait_state({"child", "router"}, 60)


def router_rows(cli) -> int:
    """Entries of `router table`: rows start with a numeric ID column."""
    return sum(1 for line in cli.cmd("router table") if re.match(r"\|\s*\d+\s*\|", line))


def test_dk_joins_and_pings_leader(xiao, dk):
    join(xiao, dk)
    addr = xiao.cmd("ipaddr mleid")[0]
    out = " ".join(dk.cmd(f"ping {addr} 64 20 1", timeout=60))  # 20 pings at 1 s interval
    m = re.search(r"(\d+) packets transmitted, (\d+) packets received", out)
    assert m, out
    assert int(m.group(1)) == 20 and int(m.group(2)) >= 19, out


def test_dk_upgrades_to_router(xiao, dk):
    join(xiao, dk)
    assert dk.wait_state({"router"}, 180) == "router"
    assert router_rows(xiao) == 2, xiao.cmd("router table")


def test_dk_reattaches_after_leader_reset(xiao, dk):
    join(xiao, dk)
    reset_board("xiao54")
    assert xiao.wait_state({"leader"}, 120) == "leader"
    assert dk.wait_state({"child", "router"}, 120) in {"child", "router"}
