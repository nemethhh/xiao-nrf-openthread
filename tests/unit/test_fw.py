import shutil
import subprocess
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


def erase_steps(argv: list[str]) -> list[str]:
    """The OpenOCD -c commands between `halt` and the image load."""
    cmds = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    return cmds[cmds.index("halt") + 1 : cmds.index("nrf54l-load x.hex")]


def test_flash_cmd_erase_commits_write_buffer_and_verifies_before_load():
    """The fill is committed (RRAMC TASKS_COMMITWRITEBUF) and read back before the image is loaded,
    so an incomplete erase aborts the flash instead of leaving half-erased settings (a partial fill,
    simulated on the DK, left an inconsistent dataset)."""
    steps = erase_steps(flash_cmd(BOARDS["dk"], Path("x.hex"), erase_range=(0x174000, 0x9000)))
    assert steps[0] == "mww 0x5004b500 0x101"  # RRAMC CONFIG: WEN=1, WRITEBUFSIZE=1
    assert steps[1] == "mww 0x174000 0xffffffff 0x2400"
    assert steps[2] == "mww 0x5004b008 0x1"  # TASKS_COMMITWRITEBUF
    assert "read_memory 0x174000 32 0x2400" in steps[3]
    assert "error" in steps[3]


@pytest.mark.parametrize("rng", [(0x174004, 0x9000), (0x174000, 0x8ffc), (0x174000, 0)])
def test_flash_cmd_erase_rejects_ranges_not_in_whole_rram_words(rng):
    with pytest.raises(ValueError):
        flash_cmd(BOARDS["dk"], Path("x.hex"), erase_range=rng)


JIMSH = shutil.which("jimsh")  # OpenOCD's own Tcl dialect


@pytest.mark.skipif(JIMSH is None, reason="jimsh not installed")
@pytest.mark.parametrize("bad_index", [None, 0, 3])
def test_flash_cmd_erase_verify_script_runs_in_jim_tcl(tmp_path, bad_index):
    verify = erase_steps(flash_cmd(BOARDS["dk"], Path("x.hex"), erase_range=(0x174000, 0x40)))[3]
    words = ["0xffffffff"] * 16
    if bad_index is not None:
        words[bad_index] = "0x12345678"
    script = tmp_path / "v.tcl"
    script.write_text(
        f"proc read_memory {{addr width count}} {{ return [list {' '.join(words)}] }}\n"
        "proc echo {msg} { puts $msg }\n"
        f"{verify}\nputs VERIFIED\n"
    )
    r = subprocess.run([JIMSH, str(script)], capture_output=True, text=True)
    if bad_index is None:
        assert r.returncode == 0 and "VERIFIED" in r.stdout, r.stdout + r.stderr
    else:
        assert r.returncode != 0 and "VERIFIED" not in r.stdout
        assert "erase" in (r.stdout + r.stderr).lower()


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


def test_router_xiao52_target_builds_thread_cli_for_xiao_ble():
    t = TARGETS["router-xiao52"]
    assert (t.app, t.board_target, t.board) == ("apps/thread-cli", "xiao_ble", "xiao52")
    argv = build_cmd(t)
    assert argv[argv.index("-d") + 1] == "build/router-xiao52"
    assert any(a.endswith("apps/thread-cli") for a in argv)
    # USB console config lives in the app's board file (picked up automatically for xiao_ble), not in NCS.
    assert (fw.REPO / "apps/thread-cli/boards/xiao_ble_nrf52840.conf").exists()
    with pytest.raises(ValueError):
        build_cmd(t, antenna="external")  # no RF switch on the XIAO nRF52840


def test_sniffer_xiao52_target_kept():
    assert TARGETS["sniffer-xiao52"].board_target == "xiao_ble"


def test_flash_uf2_tolerates_drive_vanishing_after_copy(tmp_path, monkeypatch):
    """The UF2 bootloader flashes and reboots as soon as the image is written, so the drive is gone
    before shutil.copy's chmod step (FileNotFoundError seen live). Only the data copy matters."""
    monkeypatch.setattr(fw, "REPO", tmp_path)
    monkeypatch.setattr(fw, "MEDIA_ROOT", tmp_path / "media")
    image = tmp_path / "build/router-xiao52/thread-cli/zephyr/zephyr.uf2"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"UF2\n" * 4)
    drive = tmp_path / "media/XIAO-SENSE"
    drive.mkdir(parents=True)

    def vanished(*a, **kw):
        raise FileNotFoundError("drive gone")

    monkeypatch.setattr(shutil, "copymode", vanished)
    monkeypatch.setattr(shutil, "copystat", vanished)
    fw.flash(TARGETS["router-xiao52"])
    assert (drive / "zephyr.uf2").read_bytes() == image.read_bytes()


def test_flash_uf2_without_drive_exits_with_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "MEDIA_ROOT", tmp_path / "media")
    with pytest.raises(SystemExit, match="Double-tap"):
        fw.flash(TARGETS["router-xiao52"])


def test_xiao52_board_is_the_router_port_and_sniffer_port_is_kept():
    assert BOARDS["xiao52"].serial_port == (
        "/dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_Thread_Router_B46C2A1C09259A32-if00"
    )
    assert BOARDS["xiao52"].probe == "uf2"
    assert fw.XIAO52_SNIFFER_PORT.endswith("usb-Nordic_Semiconductor_ASA_nRF_802154_Sniffer_B46C2A1C09259A32-if00")
