"""Unit tests for tools.ha_setup against a stub HaClient (no network)."""
import pytest

from tools.ha import OTBR_SLUG, XIAO_HA_DEVICE, HaError
from tools import ha_setup
from tools.ha_setup import otbr_options, plan_otbr, setup_otbr

DEV = "/dev/serial/by-id/x"


def test_otbr_options():
    assert otbr_options(DEV, 460800) == {
        "device": DEV,
        "baudrate": "460800",
        "flow_control": True,
        "nat64": True,
        "firewall": True,
        "otbr_log_level": "notice",
    }
    assert "autoflash_firmware" not in otbr_options(DEV, 460800)


class StubClient:
    def __init__(self, *, installed=True, state="stopped", options=None, datasets=None,
                 serial=(DEV,), flows=None, timeout_on=()):
        self.info = {"state": state, "options": options or {}} if installed else None
        self.datasets = datasets if datasets is not None else []
        self.serial = list(serial)
        self.flows = flows or {"otbr": [], "thread": []}
        self.timeout_on = set(timeout_on)
        self.calls = []
        self.dataset_after_confirm = None

    def serial_devices(self):
        return self.serial

    def addon_info(self, slug):
        assert slug == OTBR_SLUG
        return self.info

    def _maybe_timeout(self, name):
        if name in self.timeout_on:
            raise HaError("timeout", f"no reply to {name}")

    def install_addon(self, slug):
        self.calls.append("install")
        self.info = {"state": "stopped", "options": {}}
        self._maybe_timeout("install")

    def set_addon_options(self, slug, options):
        self.calls.append(("options", options))
        self.info["options"] = dict(options)
        self._maybe_timeout("options")

    def start_addon(self, slug):
        self.calls.append("start")
        self.info["state"] = "started"
        self._maybe_timeout("start")

    def discovery_flows(self, handler):
        return list(self.flows.get(handler, []))

    def confirm_flow(self, flow_id):
        self.calls.append(("confirm", flow_id))
        for h, fl in self.flows.items():
            self.flows[h] = [f for f in fl if f["flow_id"] != flow_id]
        if self.dataset_after_confirm is not None:
            self.datasets = self.dataset_after_confirm
        return {"type": "create_entry"}

    def thread_datasets(self):
        return list(self.datasets)

    def set_preferred_dataset(self, dataset_id):
        self.calls.append(("preferred", dataset_id))


def run(client, **kw):
    return setup_otbr(client, device=DEV, baud=460800, sleep=lambda s: None, timeout=0.05, **kw)


OTBR_DS = {"dataset_id": "d1", "source": "otbr", "preferred": False, "network_name": "N"}


def test_already_installed_no_install_call():
    c = StubClient(installed=True, datasets=[OTBR_DS])
    run(c)
    assert "install" not in c.calls
    assert ("options", otbr_options(DEV, 460800)) in c.calls
    assert "start" in c.calls
    assert ("preferred", "d1") in c.calls


def test_not_installed_installs():
    c = StubClient(installed=False, datasets=[OTBR_DS])
    run(c)
    assert c.calls[0] == "install"
    assert c.calls[1] == ("options", otbr_options(DEV, 460800))


def test_existing_other_preferred_raises_without_set_preferred():
    other = {"dataset_id": "z", "source": "manual", "preferred": True}
    c = StubClient(datasets=[other, OTBR_DS])
    with pytest.raises(RuntimeError, match="preferred"):
        run(c)
    assert not any(isinstance(x, tuple) and x[0] == "preferred" for x in c.calls)


def test_rerun_is_noop_when_otbr_already_preferred():
    ds = dict(OTBR_DS, preferred=True)
    c = StubClient(state="started", options=otbr_options(DEV, 460800), datasets=[ds])
    run(c)
    assert c.calls == []


def test_options_not_rewritten_when_equal_and_not_restarted_when_started():
    c = StubClient(state="started", options=otbr_options(DEV, 460800), datasets=[OTBR_DS])
    run(c)
    assert not any(isinstance(x, tuple) and x[0] == "options" for x in c.calls)
    assert "start" not in c.calls


def test_write_timeout_rechecks_state_without_retry():
    c = StubClient(installed=False, datasets=[OTBR_DS], timeout_on=("install", "options", "start"))
    run(c)
    assert c.calls.count("install") == 1
    assert c.calls.count("start") == 1
    assert sum(1 for x in c.calls if isinstance(x, tuple) and x[0] == "options") == 1


def test_write_timeout_with_no_effect_raises():
    class NoEffect(StubClient):
        def start_addon(self, slug):
            self.calls.append("start")
            raise HaError("timeout", "x")

    c = NoEffect(datasets=[OTBR_DS])
    with pytest.raises(TimeoutError):
        run(c)
    assert c.calls.count("start") == 1


def test_flows_confirmed_otbr_then_thread_and_dataset_awaited():
    c = StubClient(flows={"otbr": [{"flow_id": "f1"}], "thread": [{"flow_id": "f2"}]})
    c.dataset_after_confirm = [OTBR_DS]
    run(c)
    confirms = [x for x in c.calls if isinstance(x, tuple) and x[0] == "confirm"]
    assert confirms == [("confirm", "f1"), ("confirm", "f2")]
    assert ("preferred", "d1") in c.calls


def test_no_dataset_times_out():
    c = StubClient(datasets=[])
    with pytest.raises(TimeoutError):
        setup_otbr(c, device=DEV, baud=460800, sleep=lambda s: None, timeout=0)


def test_refuses_when_device_absent():
    c = StubClient(serial=["/dev/serial/by-id/other"])
    with pytest.raises(RuntimeError, match="not visible"):
        run(c)
    assert c.calls == []


def test_plan_is_read_only_and_describes_steps():
    c = StubClient(installed=False)
    lines = plan_otbr(c, device=DEV, baud=460800)
    assert c.calls == []
    text = "\n".join(lines)
    assert "install" in text and DEV in text and "flow_control" in text


def test_main_dry_run_does_not_write(monkeypatch, capsys):
    c = StubClient(installed=False, serial=[XIAO_HA_DEVICE])

    class Ctx:
        def __enter__(self):
            return c

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(ha_setup, "HaClient", lambda: Ctx())
    assert ha_setup.main([]) == 0
    assert c.calls == []
    assert "dry run" in capsys.readouterr().out.lower()


def test_main_apply_refuses_when_device_missing(monkeypatch, capsys):
    c = StubClient(installed=False, serial=[])

    class Ctx:
        def __enter__(self):
            return c

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(ha_setup, "HaClient", lambda: Ctx())
    assert ha_setup.main(["--apply"]) == 1
    assert c.calls == []
