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


def test_router_xiao52_shell_on_usb_headless_with_persistent_settings():
    """router-xiao52: shell on native USB that never waits for a host, long lines fit, settings in flash."""
    config = kconfig("router-xiao52")
    assert config.get("CONFIG_USB_DEVICE_STACK_NEXT") == "y"
    assert config.get("CONFIG_CDC_ACM_SERIAL_INITIALIZE_AT_BOOT") == "y"
    assert config.get("CONFIG_CDC_ACM_SERIAL_PRODUCT_STRING") == '"XIAO nRF52840 Thread Router"'  # fw.BOARDS["xiao52"]
    # A 64-byte RX ring dropped the rest of `ot dataset set active <tlv>` (USB delivers 64-byte bursts).
    assert int(config["CONFIG_SHELL_BACKEND_SERIAL_RX_RING_BUFFER_SIZE"]) >= int(config["CONFIG_SHELL_CMD_BUFF_SIZE"])
    for option in ("CONFIG_OPENTHREAD_MANUAL_START", "CONFIG_SETTINGS", "CONFIG_SETTINGS_ZMS", "CONFIG_OPENTHREAD_FTD",
                   "CONFIG_DEVMEM_SHELL"):  # devmem: tools.fw enters the UF2 bootloader through it
        assert config.get(option) == "y", option
    assert config.get("CONFIG_OPENTHREAD_SETTINGS_RAM") != "y"
