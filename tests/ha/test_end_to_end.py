"""Task 10 (Goal 4): the DK joins HA's Thread network and reaches the LAN through the border router."""
import ipaddress
import os
import re
import subprocess
import time

import pytest

from tests.conftest import ensure_flashed
from tests.otcli import OtError
from tools.ha import OTBR_SLUG

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
    try:
        dk.cmd(f"dataset set active {ha_dataset_hex}")  # the command text contains the network key
    except TimeoutError:
        raise TimeoutError("dataset set active timed out") from None
    except OtError as e:
        raise RuntimeError(f"dataset set active failed (error code {e.code})") from None
    for c in ("ifconfig up", "thread start"):
        dk.cmd(c)
    dk.wait_state({"child", "router"}, 90)
    return dk


def global_addrs(dk) -> list[ipaddress.IPv6Address]:
    ml = ipaddress.ip_network(
        re.search(r"Mesh Local Prefix:\s*(\S+)", " ".join(dk.cmd("dataset active"))).group(1), strict=False
    )
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
    assert preferred["network_name"] in " ".join(joined.cmd("dataset active"))


def test_dk_has_omr_address(joined):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not global_addrs(joined):
        time.sleep(3)
    assert global_addrs(joined), "no OMR address (outside mesh-local and fe80::/10) within 60 s"
    assert "border router" in " ".join(joined.cmd("netdata show")).lower() or any(
        re.match(r"\s*[0-9a-f:]+/\d+ ", l) for l in joined.cmd("netdata show")
    )


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
