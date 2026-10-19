"""Unit tests for tools.ha against an in-process fake Home Assistant WebSocket server."""
import json
import threading

import pytest
from websockets.sync.server import serve

from tools.ha import HaClient, HaError

TOKEN = "s3cr3t-token-value"


class FakeHa:
    """Fake HA: auth handshake, then answers each message via `responder(msg) -> dict`."""

    def __init__(self, responder=None, auth_ok=True):
        self.responder = responder or (lambda m: {"success": True, "result": None})
        self.auth_ok = auth_ok
        self.received = []
        self.server = serve(self._handler, "localhost", 0)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self, ws):
        ws.send(json.dumps({"type": "auth_required", "ha_version": "test"}))
        auth = json.loads(ws.recv())
        self.received.append(auth)
        if not self.auth_ok:
            ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        ws.send(json.dumps({"type": "auth_ok", "ha_version": "test"}))
        try:
            for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                reply = {"id": msg["id"], "type": "result"}
                reply.update(self.responder(msg))
                ws.send(json.dumps(reply))
        except Exception:
            pass

    def close(self):
        self.server.shutdown()


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "ha_token"
    p.write_text(TOKEN + "\n")
    return str(p)


@pytest.fixture
def make_ha(token_file):
    servers = []

    def make(responder=None, auth_ok=True):
        fake = FakeHa(responder, auth_ok)
        servers.append(fake)
        client = HaClient(f"http://localhost:{fake.port}", token_file)
        return fake, client

    yield make
    for s in servers:
        s.close()


def test_auth_handshake_sends_token(make_ha):
    fake, client = make_ha()
    with client:
        pass
    assert fake.received[0] == {"type": "auth", "access_token": TOKEN}


def test_auth_invalid_raises(make_ha):
    fake, client = make_ha(auth_ok=False)
    with pytest.raises(HaError) as ei:
        with client:
            pass
    assert TOKEN not in str(ei.value)
    assert TOKEN not in repr(ei.value)
    assert TOKEN not in repr(client)


def test_call_ids_increment_and_return_result(make_ha):
    fake, client = make_ha(lambda m: {"success": True, "result": f"r{m['id']}"})
    with client:
        assert client.call("a/b") == "r1"
        assert client.call("c/d", x=1) == "r2"
    assert [m["id"] for m in fake.received[1:]] == [1, 2]
    assert fake.received[2] == {"id": 2, "type": "c/d", "x": 1}


def test_call_error_raises_haerror(make_ha):
    fake, client = make_ha(
        lambda m: {"success": False, "error": {"code": "not_found", "message": "x"}}
    )
    with client:
        with pytest.raises(HaError) as ei:
            client.call("a/b")
    assert ei.value.code == "not_found"
    assert ei.value.message == "x"


def test_supervisor_wraps_endpoint(make_ha):
    fake, client = make_ha()
    with client:
        client.supervisor("/hardware/info")
    assert fake.received[1] == {
        "id": 1, "type": "supervisor/api", "endpoint": "/hardware/info", "method": "get",
    }


def test_supervisor_sends_data_when_given(make_ha):
    fake, client = make_ha()
    with client:
        client.set_addon_options("slug", {"device": "/dev/x"})
    msg = fake.received[1]
    assert msg["endpoint"] == "/addons/slug/options"
    assert msg["method"] == "post"
    assert msg["data"] == {"options": {"device": "/dev/x"}}


def test_addon_info_missing_returns_none(make_ha):
    fake, client = make_ha(
        lambda m: {"success": False, "error": {"code": "unknown_error", "message": "Addon not found"}}
    )
    with client:
        assert client.addon_info("core_nope") is None


def test_addon_info_returns_dict(make_ha):
    fake, client = make_ha(lambda m: {"success": True, "result": {"state": "started"}})
    with client:
        assert client.addon_info("core_x") == {"state": "started"}
    assert fake.received[1]["endpoint"] == "/addons/core_x/info"


def test_serial_devices_extracts_by_id(make_ha):
    hw = {"devices": [
        {"subsystem": "tty", "by_id": "/dev/serial/by-id/usb-A", "name": "ttyACM0"},
        {"subsystem": "tty", "by_id": "/dev/serial/by-id/usb-B", "name": "ttyACM1"},
        {"subsystem": "block", "by_id": "/dev/disk/by-id/x", "name": "sda"},
        {"subsystem": "tty", "by_id": None, "name": "ttyS0"},
    ]}
    fake, client = make_ha(lambda m: {"success": True, "result": hw})
    with client:
        assert client.serial_devices() == ["/dev/serial/by-id/usb-A", "/dev/serial/by-id/usb-B"]


def test_discovery_flows_filters_handler(make_ha):
    flows = [{"handler": "otbr", "flow_id": "1"}, {"handler": "hue", "flow_id": "2"}]
    fake, client = make_ha(lambda m: {"success": True, "result": flows})
    with client:
        assert client.discovery_flows("otbr") == [flows[0]]


def test_thread_methods(make_ha):
    def responder(m):
        if m["type"] == "thread/list_datasets":
            return {"success": True, "result": {"datasets": [{"dataset_id": "d1"}]}}
        if m["type"] == "thread/get_dataset_tlv":
            return {"success": True, "result": {"tlv": "0e08"}}
        return {"success": True, "result": None}

    fake, client = make_ha(responder)
    with client:
        assert client.thread_datasets() == [{"dataset_id": "d1"}]
        assert client.thread_dataset_tlv("d1") == "0e08"
        client.set_preferred_dataset("d1")
    assert fake.received[2] == {"id": 2, "type": "thread/get_dataset_tlv", "dataset_id": "d1"}
    assert fake.received[3] == {"id": 3, "type": "thread/set_preferred_dataset", "dataset_id": "d1"}


def test_repr_hides_token(make_ha):
    _, client = make_ha()
    assert TOKEN not in repr(client)
