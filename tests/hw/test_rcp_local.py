"""Goal 3 (local): the XIAO runs the RCP firmware and an isolated, containerised OTBR drives it.

The OTBR is ot-br-posix in Docker on an --internal network (tools/otbr-local.sh), the same software the
Home Assistant add-on wraps. The DK is the Thread test node. RCP_TEST_BAUD selects the UART baud (default RCP_BAUD).
"""

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import ensure_flashed
from tools import fw

pytestmark = pytest.mark.hw

SCRIPT = fw.REPO / "tools/otbr-local.sh"
BAUD = int(os.environ.get("RCP_TEST_BAUD", fw.RCP_BAUD))
BAD_LOG = re.compile(r"(?i)(rcp.*(reset|restor|fail|timeout)|spinel.*(error|fail|timeout))")
PING_RE = re.compile(r"(\d+) packets transmitted, (\d+) packets received")


def otbr_ctl(*args: str) -> list[str]:
    out = subprocess.run([str(SCRIPT), "ctl", *args], capture_output=True, text=True, check=True, timeout=60).stdout
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    assert lines and lines[-1] == "Done", out
    return lines[:-1]


def otbr_logs() -> str:
    return subprocess.run([str(SCRIPT), "logs"], capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module")
def otbr():
    ensure_flashed("rcp-xiao54", baud=BAUD)
    subprocess.run([str(SCRIPT), "up", str(BAUD)], check=True, timeout=120)
    yield
    subprocess.run([str(SCRIPT), "down"], check=False)


def ensure_leader() -> str:
    """Form the OTBR's network if it is not already leading; return the active dataset as hex."""
    if otbr_ctl("state") != ["leader"]:
        for c in (["dataset", "init", "new"], ["dataset", "commit", "active"], ["ifconfig", "up"], ["thread", "start"]):
            otbr_ctl(*c)
        deadline = time.monotonic() + 30
        while otbr_ctl("state") != ["leader"]:
            assert time.monotonic() < deadline, f"OTBR state {otbr_ctl('state')} after 30 s"
            time.sleep(1)
    return "".join(otbr_ctl("dataset", "active", "-x"))


def join_dk(dk) -> str:
    """Attach the DK to the OTBR network; return the OTBR's mesh-local address."""
    for c in (f"dataset set active {ensure_leader()}", "ifconfig up", "thread start"):
        dk.cmd(c)
    dk.wait_state({"child", "router"}, 60)
    return otbr_ctl("ipaddr", "mleid")[0]


def ping(dk, addr: str, size: int, count: int, interval: float) -> tuple[int, int]:
    out = " ".join(dk.cmd(f"ping {addr} {size} {count} {interval}", timeout=count * interval + 30))
    m = PING_RE.search(out)
    assert m, out
    return int(m.group(1)), int(m.group(2))


def test_otbr_sees_rcp_version(otbr):
    assert any("OPENTHREAD" in line for line in otbr_ctl("rcp", "version"))


def test_otbr_forms_network(otbr):
    ensure_leader()
    assert otbr_ctl("state") == ["leader"]


def test_dk_joins_otbr_network(otbr, dk):
    addr = join_dk(dk)
    sent, received = ping(dk, addr, 64, 20, 1)
    assert sent == 20 and received >= 19, (sent, received)


def container_state() -> str:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.RestartCount}} {{.State.Running}}", "otbr-local"], capture_output=True, text=True
    )
    return r.stdout.strip() if r.returncode == 0 else f"unavailable ({r.stderr.strip()})"


@pytest.mark.soak
def test_serial_soak(otbr, dk):
    """Pings through the RCP; the evidence file is written even when a step raises."""
    stats: list[tuple[int, int, int]] = []  # (size, sent, received), appended per phase
    error = None
    try:
        addr = join_dk(dk)
        for size, count, interval in ((64, 600, 1), (1000, 100, 0.5)):
            sent, received = ping(dk, addr, size, count, interval)
            stats.append((size, sent, received))
    except BaseException as e:  # noqa: BLE001 - recorded in the evidence file, then re-raised
        error = e
        raise
    finally:
        sent = sum(n for _, n, _ in stats)
        received = sum(r for _, _, r in stats)
        state = container_state()
        try:
            logs = otbr_logs()
        except Exception as e:  # noqa: BLE001
            logs = ""
            error = error or e
        bad = [line for line in logs.splitlines() if BAD_LOG.search(line)]
        # s6 restarts otbr-agent inside the container on exit codes 0 and 5 (e.g. a tty I/O error), which leaves
        # RestartCount 0 and Running true, so count the agent's starts and exits in the log as well.
        starts = logs.count("Starting otbr-agent...")
        exits = [line for line in logs.splitlines() if "otbr-agent exited with code" in line]
        loss = 100 * (sent - received) / sent if sent else 100.0
        report = [f"baud {BAUD}", f"loss {loss:.2f}% ({received}/{sent})", f"container {state}"]
        report += [f"ping size {sz}: {r}/{n} received" for sz, n, r in stats]
        report += [f"otbr-agent starts in log: {starts}", f"otbr-agent exit lines: {len(exits)}", *exits]
        report += [f"error: {error!r}"] if error else []
        report += ["suspicious log lines:", *bad]
        Path(fw.REPO / "build" / f"soak-{BAUD}.txt").write_text("\n".join(report) + "\n")
    assert len(stats) == 2, report
    assert loss <= 1.0, report
    assert state == "0 true", report
    assert starts == 1 and not exits, report
    assert not bad, report
