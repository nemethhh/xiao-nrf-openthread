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


def test_xiao52_board_is_the_router_port_and_sniffer_port_is_kept():
    assert BOARDS["xiao52"].serial_port == (
        "/dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_Thread_Router_B46C2A1C09259A32-if00"
    )
    assert BOARDS["xiao52"].probe == "uf2"
    assert fw.XIAO52_SNIFFER_PORT.endswith("usb-Nordic_Semiconductor_ASA_nRF_802154_Sniffer_B46C2A1C09259A32-if00")


def test_uf2_bootloader_commands_set_gpregret_magic_then_reset():
    # Adafruit nRF52 bootloader: GPREGRET (POWER 0x4000051C) = 0x57 on reset -> UF2 drive. The reset goes through
    # SCB AIRCR SYSRESETREQ, not sys_reboot (which may rewrite GPREGRET).
    assert fw.uf2_bootloader_commands() == ["devmem 0x4000051c 32 0x57", "devmem 0xe000ed0c 32 0x5fa0004"]


def uf2_env(tmp_path, monkeypatch, *, port_exists=True, drive_after=0, readback=0x57, open_error=None,
            reset_write_error=None):
    """Fake board port, lsblk/udisksctl and the XIAO drive that appears after `drive_after` polls.

    `readback` is what `devmem 0x4000051c` reports; `open_error` is raised by opening the port;
    `reset_write_error` by writing the reset line (the board resetting under the write)."""
    monkeypatch.setattr(fw, "REPO", tmp_path)
    monkeypatch.setattr(fw, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(fw.time, "sleep", lambda s: None)
    image = tmp_path / "build/router-xiao52/thread-cli/zephyr/zephyr.uf2"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"UF2")
    port = tmp_path / "ttyACM0"
    if port_exists:
        port.write_text("")
    monkeypatch.setitem(fw.BOARDS, "xiao52", fw.Board("xiao52", str(port), "uf2", None))
    log = {"written": [], "mounted": [], "polls": 0}

    class FakeSerial:
        def __init__(self, *a, **kw):
            if open_error:
                raise open_error
            log["closed"] = False

        rx = b""

        def write(self, data):
            line = data.decode().strip()
            if line:
                log["written"].append(line)
            if line == "devmem 0x4000051c":  # read-back of GPREGRET after the write
                self.rx += f"devmem 0x4000051c\r\nUsing data width 32\r\nRead value {readback:#x}\r\n".encode()
            if reset_write_error and line.startswith("devmem 0xe000ed0c"):
                raise reset_write_error

        def close(self):
            log["closed"] = True

        def read(self, n=1):
            out, self.rx = self.rx[:n], self.rx[n:]
            return out

        def reset_input_buffer(self):
            self.rx = b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    import serial

    monkeypatch.setattr(serial, "Serial", FakeSerial)

    def fake_block_device():
        log["polls"] += 1
        return "/dev/sdd" if log["polls"] > drive_after else None

    def fake_run(argv, **kw):
        assert argv[:3] == ["udisksctl", "mount", "-b"]
        log["mounted"].append(argv[3])
        (tmp_path / "media/XIAO-SENSE").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(fw, "_uf2_block_device", fake_block_device)
    monkeypatch.setattr(fw.subprocess, "run", fake_run)
    return log, tmp_path / "media/XIAO-SENSE/zephyr.uf2"


def test_flash_uf2_enters_bootloader_through_running_shell_and_mounts(tmp_path, monkeypatch):
    log, copied = uf2_env(tmp_path, monkeypatch, drive_after=2)
    fw.flash(TARGETS["router-xiao52"])
    set_magic, reset = fw.uf2_bootloader_commands()
    # The first line after opening the port can be swallowed by the shell's buffered boot output (seen live),
    # so the magic is read back before the reset.
    assert log["written"] == [set_magic, "devmem 0x4000051c", reset]
    assert log["mounted"] == ["/dev/sdd"]
    assert copied.read_bytes() == b"UF2"


def test_flash_uf2_mounts_double_tapped_drive_without_touching_port(tmp_path, monkeypatch):
    log, copied = uf2_env(tmp_path, monkeypatch, port_exists=False)
    fw.flash(TARGETS["router-xiao52"])
    assert log["written"] == []
    assert copied.exists()


def test_flash_uf2_gives_up_with_double_tap_hint(tmp_path, monkeypatch):
    log, _ = uf2_env(tmp_path, monkeypatch, port_exists=False, drive_after=10**9)
    with pytest.raises(SystemExit, match="Double-tap"):
        fw.flash(TARGETS["router-xiao52"])


def test_failed_gpregret_readback_clears_the_magic_and_does_not_reset(tmp_path, monkeypatch):
    """GPREGRET survives soft resets and sys_arch_reboot (NCS v3.4.1) does not touch it: a 0x57 left behind would
    drop the permanent router into UF2 mode at its next `ot reset` or fault reset."""
    log, _ = uf2_env(tmp_path, monkeypatch, readback=0x0, drive_after=10**9)
    with pytest.raises(SystemExit, match="Double-tap"):
        fw.flash(TARGETS["router-xiao52"])
    set_magic, reset = fw.uf2_bootloader_commands()
    assert reset not in log["written"]
    assert log["written"][-1] == "devmem 0x4000051c 32 0x0"
    assert log["closed"]


def test_uf2_entry_port_open_errors_propagate(tmp_path, monkeypatch):
    """Permission denied / port busy is not "the port vanished with the reset": report it, do not wait for a drive."""
    uf2_env(tmp_path, monkeypatch, open_error=PermissionError(13, "Permission denied"))
    with pytest.raises(PermissionError):
        fw.flash(TARGETS["router-xiao52"])


def test_uf2_entry_tolerates_port_vanishing_under_the_reset_write(tmp_path, monkeypatch):
    import serial

    log, copied = uf2_env(tmp_path, monkeypatch, drive_after=1, reset_write_error=serial.SerialException("gone"))
    fw.flash(TARGETS["router-xiao52"])
    assert copied.exists()
