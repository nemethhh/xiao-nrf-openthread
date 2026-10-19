"""The XIAO nRF52840 (router-xiao52) as a permanent Thread router in Home Assistant's network.

Needs: the XIAO nRF54L15 RCP on the HA host (the `ha` fixture skips otherwise), the XIAO nRF52840 on this PC running
router-xiao52 (skips otherwise: flashing it needs a reset double-tap, so the tests never reflash it), and the DK.
Joining goes through tools.ha_join, the same code that provisions the board. Both boards are factory-reset before
and after each test, so HA's network key does not stay on them; re-provision the nRF52840 afterwards with
`python -m tools.ha_join xiao52 --router`.
"""
import os
import re
import time

import pytest

from tests.conftest import _cli, ensure_flashed
from tests.otcli import OtError
from tools import fw, ha_join

pytestmark = [pytest.mark.ha, pytest.mark.hw]


@pytest.fixture
def preferred(ha):
    return ha_join.preferred_dataset(ha)


@pytest.fixture
def xiao52(ha):
    """Fixture order matters: `ha` skips before any hardware is touched."""
    port = fw.BOARDS["xiao52"].serial_port
    if not os.path.exists(port):
        pytest.skip(f"XIAO nRF52840 not running router-xiao52 ({port} missing): double-tap reset, "
                    "then python -m tools.fw flash router-xiao52")
    yield from _cli("xiao52")


@pytest.fixture
def joined52(ha, xiao52):
    ha_join.run("xiao52", ha, xiao52)
    return xiao52


@pytest.fixture
def router52(joined52):
    """The nRF52840 in its permanent role. Pinging it while it upgrades from child to router lost 3/10
    (its RLOC16 changes mid-way), so the DK tests start once it is a router."""
    joined52.wait_state({"router"}, 180)
    return joined52


@pytest.fixture
def dk_on_ha(ha, router52, dk_image, dk):
    ha_join.run("dk", ha, dk)
    return dk


@pytest.fixture
def dk_image(ha):
    ensure_flashed("node-dk")


def router_rows(cli) -> int:
    """Entries of `router table`: rows start with a numeric ID column."""
    return sum(1 for line in cli.cmd("router table") if re.match(r"\|\s*\d+\s*\|", line))


def same_network(cli, preferred) -> None:
    # Name and extended PAN ID only: `dataset active` output contains the network key.
    assert cli.cmd("networkname") == [preferred["network_name"]]
    assert [x.lower() for x in cli.cmd("extpanid")] == [preferred["extended_pan_id"].lower()]


def ping(dk, addr: str, count: int) -> tuple[int, int]:
    out = " ".join(dk.cmd(f"ping {addr} 64 {count} 1", timeout=count + 20))
    m = re.search(r"(\d+) packets transmitted, (\d+) packets received", out)
    assert m, out
    return int(m.group(1)), int(m.group(2))


def test_xiao52_joins_ha_network(joined52, preferred):
    assert joined52.cmd("state")[0] in {"child", "router"}
    same_network(joined52, preferred)


def test_xiao52_becomes_router(joined52):
    t0 = time.monotonic()
    assert joined52.wait_state({"router"}, 180) == "router"
    print(f"router after {time.monotonic() - t0:.0f} s; router table: {joined52.cmd('router table')}")
    assert router_rows(joined52) >= 2, joined52.cmd("router table")


def test_xiao52_rejoins_after_reset_without_commands(joined52, preferred):
    mleid = joined52.cmd("ipaddr mleid")
    reopens = joined52.reopens
    joined52.reboot()  # `ot reset`; the USB port vanishes and comes back
    t0 = time.monotonic()
    # No dataset or start commands: the router must come back on its own.
    state = joined52.wait_state({"child", "router"}, 120)
    assert joined52.reopens > reopens, "the serial port never vanished: the board did not reboot"
    print(f"back as {state} after {time.monotonic() - t0:.0f} s")
    same_network(joined52, preferred)
    assert joined52.cmd("ipaddr mleid") == mleid


def test_dk_joins_ha_network_and_pings_xiao52(dk_on_ha, router52, preferred):
    same_network(dk_on_ha, preferred)
    sent, got = ping(dk_on_ha, router52.cmd("ipaddr mleid")[0], 10)
    print(f"DK -> XIAO nRF52840 mleid: {got}/{sent} received")
    assert sent == 10 and got >= 9


def test_xiao52_headless_after_reset(dk_on_ha, router52):
    """After a reset with no host on its serial port (closed: DTR low, nobody reading), the nRF52840 still
    rejoins: the DK reaches its mesh-local EID while the port stays closed."""
    joined52 = router52
    mleid = joined52.cmd("ipaddr mleid")[0]
    joined52.reboot()
    joined52.close()  # the port is not reopened until the DK has reached the router
    t0 = time.monotonic()
    time.sleep(10)  # the reboot itself; a ping now could still be answered by the old instance
    last = None
    while time.monotonic() - t0 < 150:
        try:
            last = ping(dk_on_ha, mleid, 1)
            if last[1] == 1:
                print(f"reachable headless {time.monotonic() - t0:.0f} s after the reset")
                break
        except (TimeoutError, OtError, AssertionError) as e:
            last = repr(e)
        time.sleep(2)
    else:
        pytest.fail(f"XIAO nRF52840 not reachable from the DK within 150 s of a headless reset; last: {last}")
    assert joined52.wait_state({"child", "router"}, 30) in {"child", "router"}  # reopens the port
