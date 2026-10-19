from pathlib import Path

import pytest

from tools import fw
from tools.fw import BOARDS, RCP_BAUD, TARGETS, build_cmd, flash_cmd, flash_env, settings_range, toolchain_wrap

NCS = Path.home() / "ncs" / "v3.4.1"


def overlay_arg(argv: list[str]) -> str:
    return next((a for a in argv if a.startswith("-DEXTRA_DTC_OVERLAY_FILE=")), "")


def test_build_cmd_router_default():
    argv = build_cmd(TARGETS["router-xiao54"])
    assert argv[argv.index("-b") + 1] == "xiao_nrf54l15/nrf54l15/cpuapp"
    assert argv[argv.index("-d") + 1] == "build/router-xiao54"
    assert any(a.endswith("apps/thread-cli") for a in argv)
    assert not any("antenna-external" in a for a in argv)


def test_build_cmd_external_antenna():
    argv = build_cmd(TARGETS["router-xiao54"], antenna="external")
    assert "apps/thread-cli/overlays/antenna-external.overlay" in overlay_arg(argv)


def test_build_cmd_rcp_uses_rcp_baud():
    assert f"baud-{RCP_BAUD}.overlay" in overlay_arg(build_cmd(TARGETS["rcp-xiao54"]))
    assert "baud-115200.overlay" in overlay_arg(build_cmd(TARGETS["rcp-xiao54"], baud=115200))


def test_build_cmd_rejects_unknown_baud():
    with pytest.raises(ValueError):
        build_cmd(TARGETS["rcp-xiao54"], baud=9600)


def test_build_cmd_ncs_app_expands_to_sdk():
    argv = build_cmd(TARGETS["sniffer-xiao52"])
    assert str(NCS / "nrf/samples/peripheral/802154_sniffer") in argv


def test_flash_cmd_xiao_uses_cmsis_dap_serial():
    hex_file = Path("build/router-xiao54/merged.hex")
    argv = flash_cmd(BOARDS["xiao54"], hex_file)
    joined = " ".join(argv)
    assert "adapter serial AA2C939D" in argv
    assert str(NCS / "zephyr/boards/seeed/xiao_nrf54l15/support") in joined
    assert f"nrf54l-load {hex_file}" in argv
    assert "OPENOCD_INTERFACE" not in flash_env(BOARDS["xiao54"])


def test_flash_cmd_dk_uses_jlink():
    argv = flash_cmd(BOARDS["dk"], Path("x.hex"))
    assert flash_env(BOARDS["dk"])["OPENOCD_INTERFACE"] == "jlink"
    assert "adapter serial 001057713759" in argv
    # 1 MHz makes the J-Link OB drop memory writes mid-image; the XIAO CMSIS-DAP is unaffected.
    assert argv.index("adapter speed 500") < argv.index("init")
    assert "adapter speed 500" not in flash_cmd(BOARDS["xiao54"], Path("x.hex"))


def test_flash_cmd_erase_range():
    argv = flash_cmd(BOARDS["xiao54"], Path("x.hex"), erase_range=(0x15C000, 0x10000))
    fill = "mww 0x15c000 0xffffffff 0x4000"
    assert fill in argv
    assert argv.index(fill) < argv.index("nrf54l-load x.hex")
    assert not any(a.startswith("mww 0x15c000") for a in flash_cmd(BOARDS["xiao54"], Path("x.hex")))


def test_settings_range_parses_dts(tmp_path, monkeypatch):
    zdir = tmp_path / "build" / "router-xiao54" / "thread-cli" / "zephyr"
    zdir.mkdir(parents=True)
    (zdir / "zephyr.dts").write_text(
        "partitions {\n"
        "\tslot0_partition: partition@0 {\n\t\treg = < 0x0 0x15c000 >;\n\t};\n"
        "\tstorage_partition: partition@15c000 {\n\t\tlabel = \"storage\";\n\t\treg = < 0x15c000 0x10000 >;\n\t};\n"
        "};\n"
    )
    monkeypatch.setattr(fw, "REPO", tmp_path)
    assert settings_range(TARGETS["router-xiao54"]) == (0x15C000, 0x10000)


def test_toolchain_wrap_reads_ncs_version():
    argv = toolchain_wrap(["west", "--version"])
    assert argv[:7] == ["nrfutil", "sdk-manager", "toolchain", "launch", "--ncs-version", "v3.4.1", "--"]
    assert argv[-2:] == ["west", "--version"]


def test_build_cmd_rcp_usb_fallback_uses_cdc_conf_and_no_baud():
    argv = build_cmd(TARGETS["rcp-xiao52"])
    assert argv[argv.index("-b") + 1] == "xiao_ble"
    conf = next(a for a in argv if a.startswith("-DEXTRA_CONF_FILE="))
    path = Path(conf.removeprefix("-DEXTRA_CONF_FILE="))
    assert path.name == "usb-cdc.conf" and path.exists()
    assert "baud-" not in overlay_arg(argv)  # USB CDC has no baud rate
    with pytest.raises(ValueError):
        build_cmd(TARGETS["rcp-xiao52"], baud=115200)
