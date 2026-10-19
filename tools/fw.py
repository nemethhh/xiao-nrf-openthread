"""Build, flash, reset and recover firmware for the boards in this project.

Every build/flash command in the repo goes through this module, so the board table,
the NCS version and the RCP baud rate live in exactly one place.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO = Path(__file__).resolve().parent.parent
NCS_VERSION = (REPO / "ncs-version").read_text().strip()
NCS = Path.home() / "ncs" / NCS_VERSION
# The XIAO's OpenOCD config targets the nRF54L15 SoC and honours $OPENOCD_INTERFACE,
# so it serves the DK (same SoC, J-Link probe) as well.
OPENOCD_SUPPORT = NCS / "zephyr/boards/seeed/xiao_nrf54l15/support"

# Baud rate shared by the XIAO RCP firmware and the OTBR configuration (local and HA).
RCP_BAUD = 460800
RCP_BAUDS = (115200, 460800)


@dataclass(frozen=True)
class Board:
    name: str
    serial_port: str
    probe: Literal["cmsis-dap", "jlink", "uf2"]
    probe_serial: str | None


@dataclass(frozen=True)
class Target:
    name: str
    app: str
    board_target: str
    board: str
    overlays: tuple[str, ...] = ()
    confs: tuple[str, ...] = ()


BOARDS: dict[str, Board] = {
    "xiao54": Board(
        "xiao54",
        "/dev/serial/by-id/usb-Seeed_Studio_Seeed_Studio_XIAO_nrf54_CMSIS-DAP_AA2C939D-if02",
        "cmsis-dap",
        "AA2C939D",
    ),
    "dk": Board("dk", "/dev/serial/by-id/usb-SEGGER_J-Link_001057713759-if02", "jlink", "001057713759"),
    # Port the router-xiao52 firmware presents (thread-cli shell over USB CDC ACM, VID:PID 2fe3:0004, strings from
    # apps/thread-cli/boards/xiao_ble_nrf52840.conf, serial = the chip's HWINFO device id); verified after flashing.
    "xiao52": Board(
        "xiao52",
        "/dev/serial/by-id/usb-Seeed_Studio_XIAO_nRF52840_Thread_Router_B46C2A1C09259A32-if00",
        "uf2",
        None,
    ),
}

# Port of the same XIAO nRF52840 while it runs sniffer-xiao52 (VID 1915:154b); verified 2026-10-19. The board's
# permanent role is the Thread router, so BOARDS["xiao52"] holds the router port; re-flash the sniffer to use this.
XIAO52_SNIFFER_PORT = "/dev/serial/by-id/usb-Nordic_Semiconductor_ASA_nRF_802154_Sniffer_B46C2A1C09259A32-if00"

TARGETS: dict[str, Target] = {
    "router-xiao54": Target("router-xiao54", "apps/thread-cli", "xiao_nrf54l15/nrf54l15/cpuapp", "xiao54"),
    "node-dk": Target("node-dk", "apps/thread-cli", "nrf54l15dk/nrf54l15/cpuapp", "dk"),
    "rcp-xiao54": Target("rcp-xiao54", "apps/rcp", "xiao_nrf54l15/nrf54l15/cpuapp", "xiao54"),
    "sniffer-xiao52": Target("sniffer-xiao52", "$NCS/nrf/samples/peripheral/802154_sniffer", "xiao_ble", "xiao52"),
    # Permanent Thread router in Home Assistant's network; USB CDC ACM console from apps/thread-cli/boards/xiao_ble_nrf52840.conf.
    "router-xiao52": Target("router-xiao52", "apps/thread-cli", "xiao_ble", "xiao52"),
    "rcp-xiao52": Target("rcp-xiao52", "apps/rcp", "xiao_ble", "xiao52", confs=("apps/rcp/overlays/usb-cdc.conf",)),
}


def app_path(target: Target) -> Path:
    if target.app.startswith("$NCS/"):
        return NCS / target.app.removeprefix("$NCS/")
    return REPO / target.app


def build_cmd(
    target: Target,
    *,
    antenna: Literal["internal", "external"] = "internal",
    baud: int | None = None,
    pristine: bool = False,
) -> list[str]:
    overlays = [REPO / o for o in target.overlays]
    if antenna == "external":
        if target.board != "xiao54":
            raise ValueError(f"{target.name}: only the XIAO nRF54L15 has an antenna switch")
        overlays.append(app_path(target) / "overlays/antenna-external.overlay")
    is_uart_rcp = target.app == "apps/rcp" and target.board == "xiao54"
    if baud is not None and not is_uart_rcp:
        raise ValueError(f"{target.name}: baud applies only to the UART RCP")
    if is_uart_rcp:
        baud = RCP_BAUD if baud is None else baud
        if baud not in RCP_BAUDS:
            raise ValueError(f"unsupported RCP baud {baud}; expected one of {RCP_BAUDS}")
        overlays.append(app_path(target) / f"overlays/baud-{baud}.overlay")

    argv = ["west", "build", "-b", target.board_target, "-d", f"build/{target.name}"]
    if pristine:
        argv += ["-p", "always"]
    argv.append(str(app_path(target)))
    cmake = []
    if overlays:
        cmake.append("-DEXTRA_DTC_OVERLAY_FILE=" + ";".join(str(o) for o in overlays))
    if target.confs:
        cmake.append("-DEXTRA_CONF_FILE=" + ";".join(str(REPO / c) for c in target.confs))
    if cmake:
        argv += ["--", *cmake]
    return argv


def _image_dir(target: Target) -> Path:
    return REPO / "build" / target.name / app_path(target).name / "zephyr"


def hex_path(target: Target) -> Path:
    merged = REPO / "build" / target.name / "merged.hex"
    return merged if merged.exists() else _image_dir(target) / "zephyr.hex"


def settings_range(target: Target) -> tuple[int, int]:
    """Address and size of the settings (storage) partition of a built target."""
    dts = (_image_dir(target) / "zephyr.dts").read_text()
    m = re.search(r"storage_partition:\s*partition@\w+\s*\{[^}]*?reg\s*=\s*<\s*(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s*>", dts)
    if not m:
        raise ValueError(f"{target.name}: no storage_partition in {dts}")
    return int(m.group(1), 16), int(m.group(2), 16)


def _openocd(board: Board, *commands: str) -> list[str]:
    if board.probe == "uf2":
        raise ValueError(f"{board.name} has no debug probe; it is flashed over UF2")
    argv = ["openocd", "-s", str(OPENOCD_SUPPORT), "-f", "openocd.cfg"]
    speed = ["adapter speed 500"] if board.probe == "jlink" else []
    for c in (f"adapter serial {board.probe_serial}", *speed, "init", *commands, "shutdown"):
        argv += ["-c", c]
    return argv


def flash_env(board: Board) -> dict[str, str]:
    return {"OPENOCD_INTERFACE": "jlink"} if board.probe == "jlink" else {}


# nRF54L15 RRAM controller (GLOBAL_RRAMC_S, docs/reference/platform-seeedboards/misc/svd/nrf54l15.svd).
RRAMC = 0x5004B000
RRAMC_TASKS_COMMITWRITEBUF = RRAMC + 0x008
RRAMC_CONFIG = RRAMC + 0x500  # WEN bit 0, WRITEBUFSIZE bits 8-13 (128-bit words)
RRAM_WORD = 16  # RRAM is written in 128-bit words


def erase_commands(addr: int, size: int) -> list[str]:
    """OpenOCD commands that fill [addr, addr+size) with the erased value 0xff, then read it back.

    RRAM has no erase operation; erasing is writing 0xff. Writes go through the RRAMC write-buffer
    (WRITEBUFSIZE=1, as nrf54l-load sets it), which is committed explicitly afterwards. The read-back
    raises a Tcl error if any word is not 0xffffffff, which aborts OpenOCD before the image is loaded:
    a half-erased settings partition leaves OpenThread with an inconsistent dataset.
    """
    if size <= 0 or addr % RRAM_WORD or size % RRAM_WORD:
        raise ValueError(f"erase range {addr:#x}+{size:#x} must be whole {RRAM_WORD}-byte RRAM words")
    words = size // 4
    verify = (
        f"set bad [lsearch -all -not -exact [read_memory {addr:#x} 32 {words:#x}] 0xffffffff]; "
        f'if {{[llength $bad] > 0}} {{error "settings erase failed: [llength $bad] of {words} words at {addr:#x} '
        f'are not 0xffffffff; image not loaded"}}'
    )
    return [
        f"mww {RRAMC_CONFIG:#x} 0x101",
        f"mww {addr:#x} 0xffffffff {words:#x}",
        f"mww {RRAMC_TASKS_COMMITWRITEBUF:#x} 0x1",
        verify,
    ]


def flash_cmd(board: Board, hex_file: Path, *, erase_range: tuple[int, int] | None = None) -> list[str]:
    commands = ["halt"]
    if erase_range:
        commands += erase_commands(*erase_range)
    commands += [f"nrf54l-load {hex_file}", "reset run"]
    return _openocd(board, *commands)


def reset_cmd(board: Board) -> list[str]:
    return _openocd(board, "reset run")


def recover_cmd(board: Board) -> list[str]:
    """Erase everything and clear the access-port lock (CTRL-AP ERASEALL)."""
    return _openocd(board, "nrf54l_mass_erase", "reset run")


def toolchain_wrap(argv: list[str]) -> list[str]:
    return ["nrfutil", "sdk-manager", "toolchain", "launch", "--ncs-version", NCS_VERSION, "--", *argv]


def run(argv: list[str], env: dict[str, str] | None = None) -> None:
    full_env = os.environ | {"ZEPHYR_BASE": str(NCS / "zephyr")} | (env or {})
    full_env["PATH"] = f"{Path.home() / '.local/bin'}:{full_env['PATH']}"
    print("+", " ".join(argv), flush=True)
    subprocess.run(argv, cwd=REPO, env=full_env, check=True)


def build(target: Target, **kwargs) -> Path:
    run(toolchain_wrap(build_cmd(target, **kwargs)))
    return hex_path(target)


def flash(target: Target, *, erase_settings: bool = False) -> None:
    board = BOARDS[target.board]
    if board.probe == "uf2":
        _flash_uf2(target)
        return
    erase_range = settings_range(target) if erase_settings else None
    run(flash_cmd(board, hex_path(target), erase_range=erase_range), flash_env(board))


# Where udisks mounts the XIAO nRF52840's UF2 drive (label XIAO-SENSE; `udisksctl mount -b /dev/sdX`).
MEDIA_ROOT = Path("/run/media", os.environ.get("USER", ""))
UF2_LABEL_PREFIX = "XIAO"
UF2_WAIT = 30.0  # seconds for the drive to appear after the bootloader request

# Adafruit nRF52 bootloader: GPREGRET == 0x57 at reset means "stay in UF2 mode" (what a reset double-tap does).
NRF52_GPREGRET = 0x4000051C  # POWER->GPREGRET
UF2_MAGIC = 0x57
SCB_AIRCR = 0xE000ED0C
AIRCR_SYSRESETREQ = 0x05FA0004  # VECTKEY | SYSRESETREQ


def uf2_bootloader_commands() -> list[str]:
    """Zephyr shell (devmem) lines that reboot a running thread-cli nRF52840 into its UF2 bootloader.

    GPREGRET is retained across soft resets, and NCS v3.4.1's sys_arch_reboot does not touch it (so `kernel reboot`
    or `ot reset` would also land in the bootloader once the magic is set). The reset is a raw SYSRESETREQ through
    devmem so the whole sequence uses one mechanism. A magic left behind would send the permanent router into UF2
    mode at its next reset, so a failed attempt clears it (gpregret_clear_command).
    """
    return [f"devmem {NRF52_GPREGRET:#x} 32 {UF2_MAGIC:#x}", f"devmem {SCB_AIRCR:#x} 32 {AIRCR_SYSRESETREQ:#x}"]


def gpregret_clear_command() -> str:
    return f"devmem {NRF52_GPREGRET:#x} 32 0x0"


def _uf2_volume() -> Path | None:
    volumes = sorted(MEDIA_ROOT.glob(f"{UF2_LABEL_PREFIX}*"))
    return volumes[0] if volumes else None


def _uf2_block_device() -> str | None:
    out = subprocess.run(["lsblk", "-nrpo", "NAME,LABEL"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        name, _, label = line.partition(" ")
        if label.startswith(UF2_LABEL_PREFIX):
            return name
    return None


def _enter_uf2_bootloader(board: Board) -> bool:
    """Ask the firmware on `board` (router-xiao52's shell) to reboot into the UF2 bootloader. False if its
    port is absent (another firmware, e.g. the sniffer: then only a reset double-tap works)."""
    if not os.path.exists(board.serial_port):
        return False
    import serial

    set_magic, reset = uf2_bootloader_commands()
    print(f"+ {board.serial_port}: {set_magic}; {reset}", flush=True)
    # Errors opening the port (permission denied, busy) propagate: they are not the reset.
    port = serial.Serial(board.serial_port, 115200, timeout=0.5)
    reset_sent = False
    try:
        # Opening the port releases the shell's output held back until DTR; the first line written then
        # can be swallowed (seen live), so flush with an empty line and read the magic back before resetting.
        port.write(b"\r\n")
        time.sleep(1.0)
        port.reset_input_buffer()
        for _ in range(3):
            port.write(f"{set_magic}\r\n".encode())
            time.sleep(0.5)
            port.reset_input_buffer()
            port.write(f"devmem {NRF52_GPREGRET:#x}\r\n".encode())
            time.sleep(0.5)
            if f"Read value {UF2_MAGIC:#x}" in port.read(4096).decode(errors="replace"):
                break
        else:
            # Some write may still have landed: clear the magic, or the next reset enters the bootloader.
            port.write(f"{gpregret_clear_command()}\r\n".encode())
            time.sleep(0.5)
            print(f"{board.serial_port}: GPREGRET read-back failed; magic cleared, not resetting", flush=True)
            return False
        reset_sent = True  # the board may reset while this write is still in flight
        port.write(f"{reset}\r\n".encode())
        time.sleep(0.5)
    except OSError:
        if not reset_sent:
            raise
        # the port vanishes with the reset
    finally:
        try:
            port.close()
        except OSError:
            pass
    return True


def _wait_uf2_volume(timeout: float) -> Path | None:
    """Wait for the UF2 drive, mounting it with udisksctl when it shows up unmounted."""
    deadline = time.monotonic() + timeout
    while True:
        if volume := _uf2_volume():
            return volume
        if dev := _uf2_block_device():
            subprocess.run(["udisksctl", "mount", "-b", dev], capture_output=True, text=True)
            if volume := _uf2_volume():
                return volume
        if time.monotonic() >= deadline:
            return None
        time.sleep(1)


def _flash_uf2(target: Target) -> None:
    uf2 = _image_dir(target) / "zephyr.uf2"
    volume = _uf2_volume()
    if volume is None:
        entered = _enter_uf2_bootloader(BOARDS[target.board])
        volume = _wait_uf2_volume(UF2_WAIT if entered else 5.0)
    if volume is None:
        sys.exit(f"Double-tap reset on the XIAO nRF52840 so its UF2 drive appears, then rerun. Image: {uf2}")
    print(f"+ cp {uf2} {volume}", flush=True)
    # Data only: the bootloader flashes and reboots once the image is written, so the drive is gone
    # before shutil.copy could set permissions on the copy (FileNotFoundError seen live).
    shutil.copyfile(uf2, volume / uf2.name)


def reset(board: Board) -> None:
    run(reset_cmd(board), flash_env(board))


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m tools.fw", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("target", choices=TARGETS)
    b.add_argument("--antenna", choices=("internal", "external"), default="internal")
    b.add_argument("--baud", type=int)
    b.add_argument("--pristine", action="store_true")
    f = sub.add_parser("flash")
    f.add_argument("target", choices=TARGETS)
    f.add_argument("--erase-settings", action="store_true")
    for name in ("reset", "recover"):
        sub.add_parser(name).add_argument("board", choices=BOARDS)
    a = p.parse_args()

    if a.cmd == "build":
        build(TARGETS[a.target], antenna=a.antenna, baud=a.baud, pristine=a.pristine)
    elif a.cmd == "flash":
        flash(TARGETS[a.target], erase_settings=a.erase_settings)
    elif a.cmd == "reset":
        reset(BOARDS[a.board])
    else:
        board = BOARDS[a.board]
        run(recover_cmd(board), flash_env(board))


if __name__ == "__main__":
    main()
