import pytest

from tests.otcli import OtCli, OtError


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
