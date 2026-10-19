"""Guards on the generated Kconfig of built images (skip when a target has not been built here).

The XIAO nRF54L15 board DTS powers (rfsw-pwr, P2.3) and selects (rfsw-ctl, P2.5) the RF switch through
regulator-fixed nodes. Without the GPIO and fixed-regulator drivers neither pin is driven: the switch is
unpowered and the antenna undefined.
"""

import re

import pytest

from tools import fw

XIAO54_TARGETS = [name for name, t in fw.TARGETS.items() if t.board == "xiao54"]


def kconfig(target_name: str) -> dict[str, str]:
    path = fw._image_dir(fw.TARGETS[target_name]) / ".config"
    if not path.exists():
        pytest.skip(f"{target_name} not built ({path} missing)")
    return dict(re.findall(r"^(CONFIG_\w+)=(.*)$", path.read_text(), re.M))


def test_xiao54_targets_listed():
    assert set(XIAO54_TARGETS) == {"router-xiao54", "rcp-xiao54"}


@pytest.mark.parametrize("target_name", XIAO54_TARGETS)
def test_xiao54_image_drives_rf_switch(target_name):
    config = kconfig(target_name)
    assert config.get("CONFIG_GPIO") == "y", f"{target_name}: CONFIG_GPIO off, RF switch pins not driven"
    assert config.get("CONFIG_REGULATOR_FIXED") == "y", f"{target_name}: CONFIG_REGULATOR_FIXED off, RF switch not driven"


def test_xiao54_rcp_uart_carries_spinel_only():
    config = kconfig("rcp-xiao54")
    for option in ("CONFIG_CONSOLE", "CONFIG_UART_CONSOLE", "CONFIG_LOG", "CONFIG_SHELL"):
        assert config.get(option) != "y", f"{option}=y would put stray bytes on the Spinel UART"
