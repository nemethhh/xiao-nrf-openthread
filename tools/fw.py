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
    "xiao52": Board("xiao52", "", "uf2", None),
}

TARGETS: dict[str, Target] = {
    "router-xiao54": Target("router-xiao54", "apps/thread-cli", "xiao_nrf54l15/nrf54l15/cpuapp", "xiao54"),
    "node-dk": Target("node-dk", "apps/thread-cli", "nrf54l15dk/nrf54l15/cpuapp", "dk"),
    "rcp-xiao54": Target("rcp-xiao54", "apps/rcp", "xiao_nrf54l15/nrf54l15/cpuapp", "xiao54"),
    "sniffer-xiao52": Target("sniffer-xiao52", "$NCS/nrf/samples/peripheral/802154_sniffer", "xiao_ble", "xiao52"),
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


def flash_cmd(board: Board, hex_file: Path, *, erase_range: tuple[int, int] | None = None) -> list[str]:
    commands = ["halt"]
    if erase_range:
        addr, size = erase_range
        # Enable RRAM writes (as nrf54l-load does), then fill the range with the erased value.
        commands += ["mww 0x5004b500 0x101", f"mww {addr:#x} 0xffffffff {size // 4:#x}"]
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


def _flash_uf2(target: Target) -> None:
    uf2 = _image_dir(target) / "zephyr.uf2"
    volumes = sorted(Path("/run/media", os.environ.get("USER", "")).glob("XIAO*"))
    if not volumes:
        sys.exit(f"Double-tap reset on the XIAO nRF52840 so its UF2 drive mounts, then rerun. Image: {uf2}")
    print(f"+ cp {uf2} {volumes[0]}", flush=True)
    shutil.copy(uf2, volumes[0])


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
