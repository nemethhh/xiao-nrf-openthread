"""Task 10 (Goal 4): the DK joins HA's Thread network and reaches the LAN through the border router."""
import ipaddress
import os
import re
import subprocess
import time

import pytest

from tests.conftest import ensure_flashed
from tests.otcli import mesh_local_net, onmesh_prefixes
from tools.ha import OTBR_SLUG
from tools.ha_join import _set_dataset

pytestmark = [pytest.mark.ha, pytest.mark.hw]

LAN_PROBE = "192.168.50.10"  # the HA host; used only to pick this PC's LAN source address


@pytest.fixture
def preferred(ha):
    ds = [d for d in ha.thread_datasets() if d.get("preferred") is True]
    assert len(ds) == 1, "need exactly one preferred Thread dataset (run tools.ha_setup --apply)"
    return ds[0]


@pytest.fixture
def ha_dataset_hex(ha, preferred):
    return ha.thread_dataset_tlv(preferred["dataset_id"])


@pytest.fixture
def dk_image(ha):
    ensure_flashed("node-dk")


@pytest.fixture
def joined(ha, preferred, ha_dataset_hex, dk_image, dk):
    """Fixture order matters: `ha` skips before any hardware is touched."""
    _set_dataset(dk, ha_dataset_hex)  # the command contains the network key: kept a Secret, errors redacted
    for c in ("ifconfig up", "thread start"):
        dk.cmd(c)
    dk.wait_state({"child", "router"}, 90)
    return dk


def global_addrs(dk) -> list[ipaddress.IPv6Address]:
    """Addresses outside the mesh-local /64 and fe80::/10. Never reads `dataset active` (it prints the network key)."""
    ml = mesh_local_net(dk.cmd("ipaddr mleid"))
    out = []
    for line in dk.cmd("ipaddr"):
        try:
            a = ipaddress.IPv6Address(line.strip())
        except ValueError:
            continue
        if a not in ml and not a.is_link_local:
            out.append(a)
    return out


def lan_ipv4() -> str:
    out = subprocess.run(["ip", "-4", "route", "get", LAN_PROBE], capture_output=True, text=True, check=True).stdout
    return re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", out).group(1)


def nat64_ping(dk, count: int = 5) -> tuple[int, int]:
    out = " ".join(dk.cmd(f"ping {lan_ipv4()} 64 {count} 1", timeout=30))
    m = re.search(r"(\d+) packets transmitted, (\d+) packets received", out)
    assert m, out
    return int(m.group(1)), int(m.group(2))


def test_dk_joins_ha_network(joined, preferred):
    # Compare name and extended PAN ID only: `dataset active` output contains the network key.
    assert joined.cmd("networkname") == [preferred["network_name"]]
    assert [x.lower() for x in joined.cmd("extpanid")] == [preferred["extended_pan_id"].lower()]


def test_dk_has_omr_address(joined):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not global_addrs(joined):
        time.sleep(3)
    addrs = global_addrs(joined)
    assert addrs, "no OMR address (outside mesh-local and fe80::/10) within 60 s"
    prefixes = onmesh_prefixes(joined.cmd("netdata show"))
    assert any(a in p for a in addrs for p in prefixes), f"no address in {addrs} lies in an on-mesh prefix {prefixes}"


def test_dk_pings_lan_ipv4_via_nat64(joined):
    # If the PC firewall drops ICMP, open it temporarily and say so in the output.
    sent, got = nat64_ping(joined)
    assert sent == 5 and got >= 4


def test_dk_reattaches_after_addon_restart(ha, joined):
    if os.environ.get("HA_ALLOW_RESTART") != "1":
        pytest.skip("restart_addon is a Home Assistant write; set HA_ALLOW_RESTART=1 after the user agrees")
    ha.restart_addon(OTBR_SLUG)
    time.sleep(5)
    joined.wait_state({"child", "router"}, 120)
    sent, got = nat64_ping(joined)
    assert sent == 5 and got >= 4
