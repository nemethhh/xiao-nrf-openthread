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
        lambda m: {"success": False, "error": {"code": "unknown_error", "message": "App core_nope does not exist"}}
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


@pytest.mark.parametrize("code,message", [
    ("unauthorized", "Unauthorized"),
    ("unknown_command", "Unknown command."),
    ("unknown_error", "Supervisor is busy"),
])
def test_addon_info_other_errors_raise(make_ha, code, message):
    fake, client = make_ha(lambda m: {"success": False, "error": {"code": code, "message": message}})
    with client:
        with pytest.raises(HaError) as ei:
            client.addon_info("core_x")
    assert ei.value.code == code


def test_supervisor_timeout_passthrough(make_ha):
    fake, client = make_ha()
    with client:
        client.supervisor("/x", timeout=None)
        client.supervisor("/y", timeout=5)
        client.supervisor("/z")
    assert fake.received[1]["timeout"] is None and "timeout" in fake.received[1]
    assert fake.received[2]["timeout"] == 5
    assert "timeout" not in fake.received[3]


def test_write_calls_send_null_timeout(make_ha):
    fake, client = make_ha()
    with client:
        client.install_addon("s")
        client.start_addon("s")
        client.restart_addon("s")
        client.set_addon_options("s", {})
    for m in fake.received[1:5]:
        assert m["method"] == "post" and m["timeout"] is None


def test_call_times_out_on_silent_server(make_ha):
    def responder(m):
        import time
        time.sleep(1.5)
        return {"success": True, "result": 1}

    fake, client = make_ha(responder)
    with client:
        with pytest.raises(HaError) as ei:
            client.call("a/b", _wait=0.2)
    assert ei.value.code == "timeout"
    assert TOKEN not in str(ei.value)


def _silent_server(send_first: bool, reply_to_auth: bool = False):
    def handler(ws):
        if send_first:
            ws.send(json.dumps({"type": "auth_required"}))
        try:
            ws.recv(timeout=5)
        except Exception:
            pass
        import time
        time.sleep(2)

    server = serve(handler, "localhost", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("send_first", [False, True])
def test_auth_handshake_times_out(monkeypatch, token_file, send_first):
    import tools.ha as ha_mod

    monkeypatch.setattr(ha_mod, "READ_WAIT", 0.3)
    server = _silent_server(send_first)
    try:
        client = HaClient(f"http://localhost:{server.socket.getsockname()[1]}", token_file)
        with pytest.raises(HaError) as ei:
            with client:
                pass
        assert ei.value.code == "timeout"
        assert TOKEN not in str(ei.value)
    finally:
        server.shutdown()


def test_find_new_xiao_single():
    from tools.ha import find_new_xiao

    base = ["/dev/serial/by-id/usb-Other_1"]
    new = "/dev/serial/by-id/usb-Seeed_Studio_Seeed_Studio_XIAO_nrf54_CMSIS-DAP_AA2C939D-if02"
    assert find_new_xiao(base, base + [new, "/dev/serial/by-id/usb-Other_2"]) == new


@pytest.mark.parametrize("cur", [
    ["/dev/serial/by-id/usb-Other_1"],  # nothing new
    ["/dev/serial/by-id/usb-Other_1", "/dev/serial/by-id/usb-Seeed_XIAO_nrf54_a-if00",
     "/dev/serial/by-id/usb-Seeed_XIAO_nrf54_b-if02"],  # two new
    ["/dev/serial/by-id/usb-Other_1", "/dev/serial/by-id/usb-Other_2"],  # new but not XIAO
])
def test_find_new_xiao_raises(cur):
    from tools.ha import find_new_xiao

    with pytest.raises(ValueError):
        find_new_xiao(["/dev/serial/by-id/usb-Other_1"], cur)


def test_xiao_ha_device_constant():
    from tools.ha import XIAO_HA_DEVICE

    assert "Seeed" in XIAO_HA_DEVICE and "CMSIS-DAP" in XIAO_HA_DEVICE and "XIAO_nrf54" in XIAO_HA_DEVICE


def test_confirm_flow_posts_empty_body_with_bearer(token_file):
    import http.server

    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            seen.update(path=self.path, auth=self.headers.get("Authorization"), body=self.rfile.read(n))
            out = b'{"type": "create_entry"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("localhost", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = HaClient(f"http://localhost:{srv.server_address[1]}", token_file)
        assert client.confirm_flow("abc123") == {"type": "create_entry"}
    finally:
        srv.shutdown()
    assert seen["path"] == "/api/config/config_entries/flow/abc123"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert json.loads(seen["body"]) == {}


def test_confirm_flow_http_error_hides_token(token_file):
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("localhost", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = HaClient(f"http://localhost:{srv.server_address[1]}", token_file)
        with pytest.raises(HaError) as ei:
            client.confirm_flow("x")
    finally:
        srv.shutdown()
    assert ei.value.code == "http_404" and TOKEN not in str(ei.value)


def test_find_xiao_cli(monkeypatch, capsys):
    import tools.ha as ha_mod

    new = "/dev/serial/by-id/usb-Seeed_XIAO_nrf54_x-if02"

    class Fake:
        def __init__(self, devs):
            self.devs = devs

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def serial_devices(self):
            return self.devs

    monkeypatch.setattr(ha_mod, "HA_SERIAL_BASELINE", ["/dev/serial/by-id/usb-A"])
    monkeypatch.setattr(ha_mod, "HaClient", lambda: Fake(["/dev/serial/by-id/usb-A", new]))
    assert ha_mod.main(["find-xiao"]) == 0
    assert capsys.readouterr().out.strip() == new
    monkeypatch.setattr(ha_mod, "HaClient", lambda: Fake(["/dev/serial/by-id/usb-A"]))
    assert ha_mod.main(["find-xiao"]) == 1
    monkeypatch.setattr(ha_mod, "HaClient", lambda: Fake(["/dev/serial/by-id/usb-A", new, new + "2"]))
    assert ha_mod.main(["find-xiao"]) == 1


def test_secret_hides_value_in_repr_but_not_in_str():
    """pytest prints every frame's arguments with repr() in a failure traceback; a TLV passed as an argument
    leaked the network key that way (seen live). Secret keeps repr() redacted, also after concatenation."""
    from tools.ha import Secret

    s = Secret("0e08deadbeef")
    assert "deadbeef" not in repr(s) and "deadbeef" not in repr([s])
    assert str(s) == "0e08deadbeef" and f"x {s}" == "x 0e08deadbeef"
    cmd = "dataset set active " + s
    assert isinstance(cmd, Secret) and "deadbeef" not in repr(cmd)
    assert cmd == "dataset set active 0e08deadbeef"
    assert isinstance(s + " tail", Secret)


def test_thread_dataset_tlv_is_a_secret(make_ha):
    from tools.ha import Secret

    fake, client = make_ha(lambda m: {"success": True, "result": {"tlv": "0e08cafe"}})
    with client:
        tlv = client.thread_dataset_tlv("d1")
    assert isinstance(tlv, Secret) and "cafe" not in repr(tlv)
