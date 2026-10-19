"""Goal 1: the XIAO nRF54L15 as a standalone Thread router."""

import pytest

from tests.conftest import ensure_flashed, form_network, P2_DIR, read_gpio_p2, read_gpio_p2_out, reset_board
from tests.otcli import OtCli
from tools import fw

pytestmark = pytest.mark.hw


@pytest.fixture
def router_image():
    ensure_flashed("router-xiao54")


@pytest.fixture
def xiao(router_image, xiao):
    """The factory-reset XIAO, guaranteed to run the internal-antenna router image."""
    return xiao


RFSW_CTL_BIT = 1 << 5  # P2.5, active low: low selects the internal antenna, high the U.FL


def assert_antenna_pin(external: bool) -> None:
    """P2.5 must be driven as an output (OUT alone is also the reset value) and at the right level."""
    assert read_gpio_p2("xiao54", P2_DIR) & RFSW_CTL_BIT, "P2.5 is not an output: RF switch not driven"
    high = bool(read_gpio_p2_out("xiao54") & RFSW_CTL_BIT)
    assert high == external, f"P2.5 is {'high' if high else 'low'} but the build is {'external' if external else 'internal'}"


def test_boot_banner_reports_internal_antenna(xiao):
    reset_board("xiao54")
    assert xiao.read_until(r"thread-cli: antenna=(\w+)", 15).group(1) == "internal"
    assert_antenna_pin(external=False)


def test_forms_network_and_becomes_leader(xiao):
    form_network(xiao, "xiao-test")
    assert xiao.cmd("state") == ["leader"]


def test_network_survives_reset(xiao):
    dataset = form_network(xiao, "xiao-test")
    reset_board("xiao54")
    # No dataset or start commands: a router must come back on its own.
    assert xiao.wait_state({"leader"}, 60) == "leader"
    assert "".join(xiao.cmd("dataset active -x")) == dataset


def test_external_antenna_build():
    ensure_flashed("router-xiao54", antenna="external")
    cli = OtCli(fw.BOARDS["xiao54"].serial_port)
    try:
        cli.factory_reset()
        reset_board("xiao54")
        assert cli.read_until(r"thread-cli: antenna=(\w+)", 15).group(1) == "external"
        assert_antenna_pin(external=True)
        print(f"external build: P2 OUT={read_gpio_p2_out('xiao54'):#x} DIR={read_gpio_p2('xiao54', P2_DIR):#x}")
    finally:
        cli.close()
        ensure_flashed("router-xiao54")
