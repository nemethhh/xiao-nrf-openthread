"""tools.ha_join: write Home Assistant's preferred Thread dataset into a board over its shell."""
import pytest

from tests.otcli import OtError
from tools import ha_join

TLV = "0e080000000000010000000300000f35060004001fffe00208a98ea98ea98ea98e0708fd" + "00112233445566778899aabbccddeeff" * 2
KEY_PART = "00112233445566778899aabbccddeeff"
HA_NET = {
    "dataset_id": "01ABC",
    "network_name": "ha-thread-a98e",
    "channel": 15,
    "pan_id": "A98E",
    "extended_pan_id": "A98EA98EA98EA98E",
    "source": "Home Assistant OpenThread Border Router",
    "preferred": True,
}


class FakeHa:
    def __init__(self, datasets=None):
        self.datasets = [HA_NET] if datasets is None else datasets
        self.tlv_reads = []

    def thread_datasets(self):
        return self.datasets

    def thread_dataset_tlv(self, dataset_id):
        self.tlv_reads.append(dataset_id)
        return TLV


class FakeCli:
    """A thread-cli shell: tracks the commands and attaches once `thread start` follows a dataset."""

    def __init__(self, state="disabled", networkname="", extpanid="", fail=None, attach_as="router"):
        self.state = state
        self.networkname = networkname
        self.extpanid = extpanid
        self.fail = fail or {}
        self.attach_as = attach_as
        self.commands: list[str] = []
        self.dataset = None

    def cmd(self, command, timeout=10.0):
        self.commands.append(command)
        head = command.split()[0]
        if head in self.fail:
            raise self.fail[head]
        if command.startswith("dataset set active "):
            self.dataset = command.split()[-1]
            return []
        if command == "thread start":
            if self.dataset:
                self.state = self.attach_as
                self.networkname, self.extpanid = HA_NET["network_name"], HA_NET["extended_pan_id"].lower()
            return []
        if command == "thread stop":
            self.state = "disabled"
            return []
        return {
            "state": [self.state],
            "networkname": [self.networkname],
            "extpanid": [self.extpanid],
            "rloc16": ["5c00"],
            "ipaddr mleid": ["fd76:a5d1:fcb0:1707:1234:5678:9abc:def0"],
        }.get(command, [])

    def wait_state(self, states, timeout, poll_timeout=3.0):
        self.commands.append(f"<wait {sorted(states)} {timeout:g}>")
        if self.state in states:
            return self.state
        raise TimeoutError(f"state {self.state!r} not in {states} after {timeout}s")


def run(cli, ha=None, capsys=None, **kw):
    rc = ha_join.run("xiao52", ha or FakeHa(), cli, **kw)
    out = capsys.readouterr() if capsys else None
    return rc, out


def test_preferred_dataset_picks_the_single_preferred():
    other = dict(HA_NET, dataset_id="02", preferred=False, network_name="other")
    assert ha_join.preferred_dataset(FakeHa([other, HA_NET]))["dataset_id"] == "01ABC"


@pytest.mark.parametrize("datasets", [[], [dict(HA_NET, preferred=False)], [HA_NET, dict(HA_NET, dataset_id="02")]])
def test_preferred_dataset_refuses_zero_or_several(datasets):
    with pytest.raises(RuntimeError, match="preferred"):
        ha_join.preferred_dataset(FakeHa(datasets))


def test_join_sends_dataset_then_starts_and_waits(capsys):
    cli = FakeCli()
    rc, out = run(cli, capsys=capsys)
    assert rc == 0
    writes = [c for c in cli.commands if not c.startswith("<") and c.split()[0] in ("dataset", "ifconfig", "thread")]
    assert writes == [f"dataset set active {TLV}", "ifconfig up", "thread start"]
    assert any(c.startswith("<wait ['child', 'router']") for c in cli.commands)
    assert "ha-thread-a98e" in out.out and "router" in out.out


def test_never_prints_the_tlv_or_key(capsys):
    rc, out = run(FakeCli(), capsys=capsys)
    assert rc == 0
    for text in (out.out, out.err):
        assert TLV not in text and KEY_PART not in text


@pytest.mark.parametrize("error", [OtError(7, "InvalidArgs"), TimeoutError(f"no 'Done' for 'ot dataset set active {TLV}'")])
def test_dataset_errors_are_reported_without_the_tlv(error, capsys):
    cli = FakeCli(fail={"dataset": error})
    with pytest.raises(RuntimeError) as exc:
        run(cli)
    assert TLV not in str(exc.value) and KEY_PART not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert "thread start" not in cli.commands


def test_stops_thread_first_when_board_is_on_another_network():
    cli = FakeCli(state="leader", networkname="xiao-test", extpanid="1111111111111111")
    assert run(cli)[0] == 0
    first_write = next(c for c in cli.commands if c.split()[0] in ("dataset", "thread", "ifconfig"))
    assert first_write == "thread stop"
    assert cli.commands.index("thread stop") < cli.commands.index(f"dataset set active {TLV}")


def test_already_joined_board_is_left_alone(capsys):
    cli = FakeCli(state="child", networkname="ha-thread-a98e", extpanid="a98ea98ea98ea98e")
    ha = FakeHa()
    rc, out = run(cli, ha, capsys=capsys)
    assert rc == 0
    assert not any(c.split()[0] in ("dataset", "thread", "ifconfig") for c in cli.commands if not c.startswith("<"))
    assert ha.tlv_reads == []  # the key is not even fetched
    assert "already" in out.out


def test_dry_run_reads_but_never_writes(capsys):
    cli = FakeCli()
    ha = FakeHa()
    rc, out = run(cli, ha, capsys=capsys, dry_run=True)
    assert rc == 0
    assert set(cli.commands) <= {"state", "networkname", "extpanid"}
    assert ha.tlv_reads == []
    assert "dataset set active <HA preferred dataset TLV, not shown>" in out.out
    assert "dry run" in out.out.lower()


def test_router_flag_waits_for_router_180s():
    cli = FakeCli()
    run(cli, router=True)
    assert "<wait ['router'] 180>" in cli.commands


def test_router_flag_fails_if_board_stays_child():
    cli = FakeCli(attach_as="child")
    with pytest.raises(TimeoutError):
        run(cli, router=True)


def test_wrong_network_after_start_is_an_error():
    class WrongNet(FakeCli):
        def cmd(self, command, timeout=10.0):
            out = super().cmd(command, timeout)
            return ["someone-else"] if command == "networkname" and self.dataset else out

    with pytest.raises(RuntimeError, match="network name"):
        run(WrongNet())


def test_main_rejects_unknown_board(capsys):
    with pytest.raises(SystemExit):
        ha_join.main(["nope"])
