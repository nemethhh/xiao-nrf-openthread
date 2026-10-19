"""Drive the OpenThread CLI (`ot ...` in the Zephyr shell) over a serial port."""

from __future__ import annotations

import ipaddress
import re
import termios
import time
from typing import Callable

import serial

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ORPHAN_SGR = re.compile(r"^[0-9;]*m(?=uart:~\$)")  # prompt whose leading `\x1b[` was flushed away
PROMPT = re.compile(r"uart:~\$ ?")  # also redrawn mid-line around asynchronous output
LOG_LINE = re.compile(r"^\[\d\d:\d\d:\d\d\.\d{3},\d{3}\] <\w+>")
ERROR = re.compile(r"^Error (\d+): (.*)$")
# Commands whose text or output can carry the network key: never echo them in exceptions.
SECRET_COMMANDS = ("dataset", "networkkey")
# What pyserial raises on a tty that vanished: SerialException (an OSError) or, from tcflush, termios.error.
PORT_ERRORS = (OSError, termios.error)


class OtError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"Error {code}: {message}")
        self.code = code


class PortLost(TimeoutError):
    """The serial port vanished (a native-USB board rebooting and re-enumerating). A TimeoutError, so
    callers that already retry lost commands (wait_state, factory_reset) retry this too."""


class OtCli:
    """`port` may disappear and come back: boards on native USB (XIAO nRF52840) re-enumerate on every
    reboot. A lost port is reopened by the next command; until then commands raise PortLost."""

    def __init__(self, port: str, baudrate: int = 115200, *, serial_factory: Callable = serial.Serial):
        self._port = port
        self._baudrate = baudrate
        self._factory = serial_factory
        self._ser = None
        self._buf = ""
        self.reopens = 0  # times a lost port was opened again (a native-USB board rebooted)
        self._open(serial_factory(port, baudrate=baudrate, timeout=0.05))

    def _open(self, ser) -> None:
        """On open a board on native USB sends the output it held back until DTR, and the host tty echoes it
        back before pyserial switches to raw mode: the shell then has junk (`*** Booting nRF ...`) in front of
        the next command (seen live: `***: command not found`). An empty line flushes it."""
        self._ser = ser
        self._buf = ""
        try:
            ser.write(b"\r\n")
            time.sleep(0.3)
            ser.reset_input_buffer()
        except PORT_ERRORS as e:
            raise self._lost(e) from None

    def _serial(self):
        if self._ser is None:
            try:
                ser = self._factory(self._port, baudrate=self._baudrate, timeout=0.05)
            except PORT_ERRORS as e:  # serial.SerialException is an OSError
                time.sleep(0.5)  # the device is still re-enumerating
                raise PortLost(f"serial port not back yet: {type(e).__name__}") from None
            self.reopens += 1
            self._open(ser)
        return self._ser

    def _lost(self, e: Exception) -> PortLost:
        try:
            self._ser.close()
        except PORT_ERRORS:
            pass
        self._ser = None
        return PortLost(f"serial port lost: {type(e).__name__}")

    def _write(self, data: bytes) -> None:
        ser = self._serial()
        try:
            ser.write(data)
        except PORT_ERRORS as e:
            raise self._lost(e) from None

    def _read_chunk(self) -> bytes:
        ser = self._serial()
        try:
            return ser.read(ser.in_waiting or 1)
        except PORT_ERRORS as e:
            raise self._lost(e) from None

    def _next_line(self, deadline: float) -> str | None:
        """Return the next cleaned, complete line, or None once the deadline passes."""
        while True:
            if "\n" in self._buf:
                raw, self._buf = self._buf.split("\n", 1)
                return PROMPT.sub("", ORPHAN_SGR.sub("", ANSI.sub("", raw))).strip()
            if time.monotonic() >= deadline:
                return None
            chunk = self._read_chunk()
            if chunk:
                self._buf += chunk.decode(errors="replace")
            else:
                time.sleep(0.01)

    def cmd(self, command: str, timeout: float = 10.0) -> list[str]:
        ser = self._serial()
        try:
            ser.reset_input_buffer()
        except PORT_ERRORS as e:
            raise self._lost(e) from None
        self._buf = ""
        line_sent = "ot " + command  # not an f-string: keeps a Secret command (tools.ha.Secret) redacted
        try:  # inline, not self._write(data): a traceback would print that frame's argument, the key for dataset commands
            ser.write(f"{line_sent}\r\n".encode())
        except PORT_ERRORS as e:
            raise self._lost(e) from None
        deadline = time.monotonic() + timeout
        out: list[str] = []
        while (line := self._next_line(deadline)) is not None:
            if not line or line == line_sent or LOG_LINE.match(line):
                continue
            if line == "Done":
                return out
            if m := ERROR.match(line):
                raise OtError(int(m.group(1)), m.group(2))
            out.append(line)
        if command.split()[0] in SECRET_COMMANDS:
            shown = " ".join(line_sent.split()[:3]) + " ..."
            raise TimeoutError(f"no 'Done' for {shown!r} within {timeout}s ({len(out)} lines of output withheld)")
        raise TimeoutError(f"no 'Done' for {line_sent!r} within {timeout}s; got {out}")

    def read_until(self, pattern: str, timeout: float) -> re.Match:
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while (line := self._next_line(deadline)) is not None:
            if m := regex.search(line):
                return m
        raise TimeoutError(f"{pattern!r} not seen within {timeout}s")

    def wait_state(self, states: set[str], timeout: float, poll_timeout: float = 3.0) -> str:
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            try:
                state = self.cmd("state", timeout=poll_timeout)[0]
            except TimeoutError:
                continue  # node rebooting: the command was lost, ask again
            if state in states:
                return state
            time.sleep(1)
        raise TimeoutError(f"state {state!r} not in {states} after {timeout}s")

    def factory_reset(self, timeout: float = 40.0) -> None:
        """Erase Thread settings; the node reboots and comes back with OpenThread disabled.

        A node that is still booting (e.g. autostarting a stored network) swallows the request,
        so keep re-sending until the shell answers `disabled` after a reset was sent.
        """
        deadline = time.monotonic() + timeout
        sent = False
        while time.monotonic() < deadline:
            try:
                state = self.cmd("state", timeout=2)
            except (TimeoutError, OtError):
                time.sleep(0.5)
                continue  # shell not up yet
            if sent and state == ["disabled"]:
                return
            try:
                self._write(b"ot factoryreset\r\n")
            except PortLost:
                continue
            sent = True
            time.sleep(2)  # let the reboot happen before asking again
        raise TimeoutError("node did not come back after factoryreset")

    def reboot(self) -> None:
        """`ot reset`: reboot the node without waiting for it (no reply comes back before the reboot)."""
        self._write(b"ot reset\r\n")

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None


NETDATA_SECTIONS = {"Prefixes:", "Routes:", "Services:", "Contexts:", "Commissioning:"}


def onmesh_prefixes(netdata: list[str]) -> list[ipaddress.IPv6Network]:
    """On-mesh prefixes (flag `o`) from the `Prefixes:` section of `ot netdata show` output.

    Prefix lines read `<prefix>/<len> <flags> <preference> <rloc16>`, e.g. `fd76:a5d1:fcb0:1707::/64 paos low 5800`.
    """
    section, out = None, []
    for line in netdata:
        if line in NETDATA_SECTIONS:
            section = line
            continue
        fields = line.split()
        if section == "Prefixes:" and len(fields) >= 4 and "o" in fields[1]:
            out.append(ipaddress.IPv6Network(fields[0], strict=False))
    return out


def mesh_local_net(mleid: list[str]) -> ipaddress.IPv6Network:
    """The mesh-local /64 from `ot ipaddr mleid` output (an address in the mesh-local prefix)."""
    return ipaddress.IPv6Network(f"{mleid[0].strip()}/64", strict=False)
