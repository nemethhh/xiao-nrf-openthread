import ipaddress

import pytest

from tests.otcli import OtCli, OtError, mesh_local_net, onmesh_prefixes


class FakeSerial:
    """Answers each written line with a canned response, like the Zephyr shell would."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.written: list[str] = []
        self._rx = b""

    def write(self, data: bytes) -> int:
        line = data.decode().strip()
        self.written.append(line)
        self._rx += self.responses.get(line, "").encode()
        return len(data)

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def read(self, n: int = 1) -> bytes:
        out, self._rx = self._rx[:n], self._rx[n:]
        return out

    def reset_input_buffer(self) -> None:
        self._rx = b""

    def close(self) -> None:
        pass


def cli_with(responses: dict[str, str]) -> OtCli:
    fake = FakeSerial(responses)
    return OtCli("fake", serial_factory=lambda *a, **kw: fake)


def test_cmd_returns_lines_before_done():
    cli = cli_with({"ot state": "ot state\r\nleader\r\nDone\r\n"})
    assert cli.cmd("state") == ["leader"]


def test_cmd_raises_ot_error():
    cli = cli_with({"ot channel 99": "Error 7: InvalidArgs\r\n"})
    with pytest.raises(OtError) as exc:
        cli.cmd("channel 99")
    assert exc.value.code == 7


def test_cmd_strips_prompt_and_ansi():
    cli = cli_with({"ot panid": "\x1b[1;32muart:~$ \x1b[mot panid\r\n\x1b[m0xface\r\nDone\r\nuart:~$ "})
    assert cli.cmd("panid") == ["0xface"]


def test_cmd_ignores_log_lines():
    cli = cli_with({"ot state": "[00:00:12.345,678] <inf> net_l2_openthread: State changed!\r\nchild\r\nDone\r\n"})
    assert cli.cmd("state") == ["child"]


def test_cmd_timeout():
    cli = cli_with({"ot state": "leader\r\n"})
    with pytest.raises(TimeoutError):
        cli.cmd("state", timeout=0.2)


def test_read_until_matches_boot_banner():
    fake = FakeSerial({})
    cli = OtCli("fake", serial_factory=lambda *a, **kw: fake)
    fake._rx = b"*** Booting nRF Connect SDK ***\r\nthread-cli: antenna=internal\r\n"
    assert cli.read_until(r"thread-cli: antenna=(\w+)", 1).group(1) == "internal"


def test_wait_state_retries_when_node_is_still_booting():
    """A command written while the node reboots is lost; polling must carry on."""
    replies = iter(["", "ot state\r\ndetached\r\nDone\r\n", "ot state\r\nleader\r\nDone\r\n"])

    class BootingSerial(FakeSerial):
        def write(self, data):
            self._rx += next(replies).encode()
            return len(data)

    fake = BootingSerial({})
    cli = OtCli("fake", serial_factory=lambda *a, **kw: fake)
    assert cli.wait_state({"leader"}, 30, poll_timeout=0.2) == "leader"


def test_factory_reset_retries_when_first_request_is_lost():
    """A node that autostarts a stored network swallows the first factoryreset while booting."""

    class LossySerial(FakeSerial):
        def __init__(self):
            super().__init__({})
            self.resets = 0
            self.state = "leader"

        def write(self, data):
            line = data.decode().strip()
            self.written.append(line)
            if line == "ot factoryreset":
                self.resets += 1
                if self.resets >= 2:
                    self.state = "disabled"
            elif line == "ot state":
                self._rx += f"ot state\r\n{self.state}\r\nDone\r\n".encode()
            return len(data)

    fake = LossySerial()
    cli = OtCli("fake", serial_factory=lambda *a, **kw: fake)
    cli.factory_reset()
    assert fake.resets == 2
    assert fake.state == "disabled"


def test_cmd_strips_prompt_reprinted_inside_async_output():
    # The shell redraws its prompt around asynchronous output such as `ot ping` replies.
    cli = cli_with({
        "ot ping x": "ot ping x\r\n"
        "16 bytes from uart:~$ fd00::1uart:~$ : icmp_seq=1\r\n"
        "1 packets transmitted, 1 packets received.uart:~$ \r\n"
        "Doneuart:~$ \r\n"
    })
    assert cli.cmd("ping x") == ["16 bytes from fd00::1: icmp_seq=1", "1 packets transmitted, 1 packets received."]


KEY = "00112233445566778899aabbccddeeff"


def test_cmd_timeout_keeps_partial_output_for_ordinary_commands():
    cli = cli_with({"ot state": "leader\r\n"})
    with pytest.raises(TimeoutError) as exc:
        cli.cmd("state", timeout=0.2)
    assert "leader" in str(exc.value)


def test_cmd_timeout_hides_dataset_output():
    """`dataset active` prints the network key; a timeout must not carry it into pytest output."""
    cli = cli_with({"ot dataset active": f"Active Timestamp: 1\r\nNetwork Key: {KEY}\r\n"})
    with pytest.raises(TimeoutError) as exc:
        cli.cmd("dataset active", timeout=0.2)
    assert KEY not in str(exc.value)
    assert "dataset" in str(exc.value)


def test_cmd_timeout_hides_dataset_command_arguments():
    tlvs = "0e080000000000010000" + KEY
    cli = cli_with({})
    with pytest.raises(TimeoutError) as exc:
        cli.cmd(f"dataset set active {tlvs}", timeout=0.2)
    assert tlvs not in str(exc.value) and KEY not in str(exc.value)


NETDATA = [
    "Prefixes:",
    "fd76:a5d1:fcb0:1707::/64 paos low 5800",
    "fd00:dead:beef:cafe::/64 paros med dc00",
    "Routes:",
    "fd49:7770:7fc5:0::/64 s med 4000",
    "::/0 s med 4000",
    "Services:",
    "44970 5d c000 s 4000",
    "Contexts:",
    "fd76:a5d1:fcb0:1707::/64 1 c",
]


def test_onmesh_prefixes_reads_only_prefix_section_with_o_flag():
    assert onmesh_prefixes(NETDATA) == [
        ipaddress.IPv6Network("fd76:a5d1:fcb0:1707::/64"),
        ipaddress.IPv6Network("fd00:dead:beef:cafe::/64"),
    ]


def test_onmesh_prefixes_ignores_prefix_without_o_flag_and_routes():
    assert onmesh_prefixes(["Prefixes:", "fd11::/64 pas med 1000", "Routes:", "fd22::/64 s med 4000"]) == []


def test_mesh_local_net_from_mleid():
    assert mesh_local_net(["fdde:ad00:beef:0:558:f56b:d688:799"]) == ipaddress.IPv6Network("fdde:ad00:beef::/64")


def test_cmd_tolerates_truncated_prompt_escape_before_echo():
    """Flushing the input buffer can cut the previous prompt's SGR escape in half."""
    cli = cli_with({"ot networkname": "32muart:~$ \x1b[mot networkname\r\nha-thread-a98e\r\nDone\r\n"})
    assert cli.cmd("networkname") == ["ha-thread-a98e"]


def test_cmd_tolerates_truncated_prompt_escape_with_semicolon():
    cli = cli_with({"ot ipaddr mleid": "1;32muart:~$ \x1b[mot ipaddr mleid\r\nfdde:ad00:beef:0:8c58:70b9:dafd:7c23\r\nDone\r\n"})
    assert cli.cmd("ipaddr mleid") == ["fdde:ad00:beef:0:8c58:70b9:dafd:7c23"]


class UsbBoard:
    """A board on native USB (XIAO nRF52840): a reboot makes its tty vanish and come back.

    `open()` is the serial factory. After `ot factoryreset` / `ot reset` the open port dies (reads and
    writes raise SerialException, as pyserial does once the device is gone) and the next `gone_opens`
    attempts to reopen fail, as while the device re-enumerates.
    """

    def __init__(self, state="router", gone_opens=2):
        self.state = state
        self.gone_opens = gone_opens
        self.pending_gone = 0
        self.opens = 0
        self.ports: list = []
        self.written: list[str] = []

    def open(self, *a, **kw):
        import serial

        if self.pending_gone:
            self.pending_gone -= 1
            raise serial.SerialException("could not open port: No such file or directory")
        self.opens += 1
        board = self

        class Port(FakeSerial):
            dead = False

            def _check(self):
                if self.dead:
                    raise serial.SerialException("device reports readiness to read but returned no data")

            def write(self, data):
                self._check()
                line = data.decode().strip()
                board.written.append(line)
                if line in ("ot factoryreset", "ot reset"):
                    if line == "ot factoryreset":
                        board.state = "disabled"
                    board.pending_gone = board.gone_opens
                    for p in board.ports:
                        p.dead = True
                elif line == "ot state":
                    self._rx += f"ot state\r\n{board.state}\r\nDone\r\n".encode()
                return len(data)

            def read(self, n=1):
                self._check()
                return super().read(n)

            @property
            def in_waiting(self):
                self._check()
                return len(self._rx)

        port = Port({})
        self.ports.append(port)
        return port


def test_factory_reset_survives_usb_reenumeration():
    board = UsbBoard()
    cli = OtCli("usb", serial_factory=board.open)
    cli.factory_reset(timeout=20)
    assert board.state == "disabled"
    assert board.opens >= 2  # reopened after the port vanished
    assert cli.cmd("state") == ["disabled"]


def test_wait_state_survives_usb_reboot():
    board = UsbBoard(state="child")
    cli = OtCli("usb", serial_factory=board.open)
    cli.reboot()
    assert "ot reset" in board.written
    assert cli.wait_state({"child", "router"}, 20, poll_timeout=0.2) == "child"
    assert board.opens == 2


def test_port_lost_is_a_timeout_error():
    board = UsbBoard(gone_opens=100)
    cli = OtCli("usb", serial_factory=board.open)
    cli.reboot()
    with pytest.raises(TimeoutError):
        cli.cmd("state", timeout=0.5)
