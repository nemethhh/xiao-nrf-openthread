"""Home Assistant WebSocket client (sync). The token is never printed, logged or put in errors.

CLI is read-only: python -m tools.ha info|serial|find-xiao|addon-info [slug]|datasets
Writes (install/options/start/restart, confirm_flow, set_preferred_dataset) exist only as Python API.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from websockets.sync.client import connect

OTBR_SLUG = "core_openthread_border_router"
READ_WAIT = 30.0  # seconds to wait for a reply to a read
WRITE_WAIT = 600.0  # seconds to wait for a reply to install/start/restart
_UNSET: Any = object()

# Predicted HA-side path of the XIAO RCP's UART bridge. HA OS uses the same udev by-id naming as the dev
# host; Task 8 confirms it by diffing `python -m tools.ha serial` against the pre-plug baseline
# (find_new_xiao). Interface 02 is the SAMD11 bridge's CDC-ACM UART.
XIAO_HA_DEVICE = "/dev/serial/by-id/usb-Seeed_Studio_Seeed_Studio_XIAO_nrf54_CMSIS-DAP_AA2C939D-if02"

# HA serial by-id devices recorded BEFORE the XIAO was plugged into the HA host (Task 7: only /dev/ttyS0-3,
# which have no by-id entry). `python -m tools.ha find-xiao` diffs the live list against this.
HA_SERIAL_BASELINE: list[str] = []


def find_new_xiao(baseline: list[str], current: list[str]) -> str:
    """The single by-id entry in `current` (not in `baseline`) containing "XIAO_nrf54"; raise on 0 or >1."""
    new = [d for d in current if d not in set(baseline) and "XIAO_nrf54" in d]
    if len(new) != 1:
        raise ValueError(f"expected exactly one new XIAO_nrf54 serial device, found {len(new)}: {new}")
    return new[0]


class Secret(str):
    """A str whose repr() is redacted (e.g. a Thread dataset TLV, which holds the network key).

    pytest prints the arguments of every frame in a failure traceback with repr(), so a plain str TLV passed
    to a function leaks there. str() and f-strings still give the value; concatenation stays a Secret.
    """

    def __repr__(self) -> str:
        return "<secret, redacted>"

    def __add__(self, other: str) -> "Secret":
        return Secret(str.__add__(self, other))

    def __radd__(self, other: str) -> "Secret":
        return Secret(str.__add__(other, self))


class HaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HaClient:
    def __init__(self, base_url: str = "http://192.168.50.10", token_path: str = "ha_token"):
        u = urlparse(base_url)
        scheme = "wss" if u.scheme == "https" else "ws"
        self._base_url = base_url.rstrip("/")
        self.ws_url = f"{scheme}://{u.netloc}/api/websocket"
        self._token = Path(token_path).read_text().strip()
        self._ws = None
        self._cm = None
        self._id = 0

    def __repr__(self) -> str:
        return f"HaClient(ws_url={self.ws_url!r})"

    def __enter__(self) -> "HaClient":
        self._cm = connect(self.ws_url, max_size=None)
        self._ws = self._cm.__enter__()
        try:
            deadline = time.monotonic() + READ_WAIT
            first = json.loads(self._recv(deadline, "auth_required"))
            if first.get("type") != "auth_required":
                raise HaError("protocol", f"unexpected first message type {first.get('type')!r}")
            self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            reply = json.loads(self._recv(deadline, "auth_ok"))
            if reply.get("type") != "auth_ok":
                raise HaError("auth_invalid", "authentication rejected by Home Assistant")
        except BaseException:
            self.close()
            raise
        return self

    def _recv(self, deadline: float, what: str) -> str | bytes:
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise TimeoutError
            return self._ws.recv(timeout=remaining)
        except TimeoutError:
            raise HaError("timeout", f"no {what} from Home Assistant within {READ_WAIT:g}s") from None

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._ws is not None:
            self._cm.__exit__(None, None, None)
            self._ws = None

    def call(self, msg_type: str, *, _wait: float = READ_WAIT, **payload) -> Any:
        if self._ws is None:
            raise HaError("not_connected", "use HaClient as a context manager")
        self._id += 1
        msg_id = self._id
        self._ws.send(json.dumps({"id": msg_id, "type": msg_type, **payload}))
        deadline = time.monotonic() + _wait
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError:
                raise HaError("timeout", f"no reply to {msg_type} within {_wait:g}s") from None
            reply = json.loads(raw)
            if reply.get("id") != msg_id:
                continue
            if reply.get("success"):
                return reply.get("result")
            err = reply.get("error") or {}
            raise HaError(str(err.get("code", "unknown")), str(err.get("message", "")))

    def supervisor(
        self,
        endpoint: str,
        method: str = "get",
        data: dict | None = None,
        timeout: Any = _UNSET,
        wait: float = READ_WAIT,
    ) -> Any:
        """`timeout` is HA's own limit for the Supervisor call (omitted = HA default ~10 s, None =
        no limit); `wait` is how long we wait for HA's reply."""
        payload: dict[str, Any] = {"endpoint": endpoint, "method": method}
        if data is not None:
            payload["data"] = data
        if timeout is not _UNSET:
            payload["timeout"] = timeout
        return self.call("supervisor/api", _wait=wait, **payload)

    def serial_devices(self) -> list[str]:
        info = self.supervisor("/hardware/info")
        return [
            d["by_id"]
            for d in info.get("devices", [])
            if d.get("subsystem") == "tty" and d.get("by_id")
        ]

    def addon_info(self, slug: str) -> dict | None:
        try:
            return self.supervisor(f"/addons/{slug}/info")
        except HaError as e:
            # Supervisor: code unknown_error, message "App <slug> does not exist" (verified live).
            if e.code == "unknown_error" and "does not exist" in e.message:
                return None
            raise

    def install_addon(self, slug: str) -> Any:
        return self.supervisor(f"/store/addons/{slug}/install", "post", timeout=None, wait=WRITE_WAIT)

    def set_addon_options(self, slug: str, options: dict) -> Any:
        return self.supervisor(f"/addons/{slug}/options", "post", {"options": options}, timeout=None, wait=WRITE_WAIT)

    def start_addon(self, slug: str) -> Any:
        return self.supervisor(f"/addons/{slug}/start", "post", timeout=None, wait=WRITE_WAIT)

    def restart_addon(self, slug: str) -> Any:
        return self.supervisor(f"/addons/{slug}/restart", "post", timeout=None, wait=WRITE_WAIT)

    def discovery_flows(self, handler: str) -> list[dict]:
        flows = self.call("config_entries/flow/progress")
        return [f for f in flows if f.get("handler") == handler]

    def confirm_flow(self, flow_id: str) -> dict:
        """Submit an empty step to a discovery flow. HA has no WS command for this, so use REST."""
        req = urllib.request.Request(
            f"{self._base_url}/api/config/config_entries/flow/{flow_id}",
            data=b"{}",
            method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise HaError(f"http_{e.code}", "confirm_flow rejected") from None

    def thread_datasets(self) -> list[dict]:
        return self.call("thread/list_datasets")["datasets"]

    def thread_dataset_tlv(self, dataset_id: str) -> str:
        return Secret(self.call("thread/get_dataset_tlv", dataset_id=dataset_id)["tlv"])

    def set_preferred_dataset(self, dataset_id: str) -> None:
        self.call("thread/set_preferred_dataset", dataset_id=dataset_id)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    try:
        with HaClient() as ha:
            if cmd == "info":
                print(ha.call("get_config")["version"])
            elif cmd == "serial":
                for d in ha.serial_devices():
                    print(d)
            elif cmd == "find-xiao":
                try:
                    print(find_new_xiao(HA_SERIAL_BASELINE, ha.serial_devices()))
                except ValueError as e:
                    print(f"find-xiao: {e}", file=sys.stderr)
                    return 1
            elif cmd == "addon-info":
                slug = argv[1] if len(argv) > 1 else OTBR_SLUG
                info = ha.addon_info(slug)
                if info is None:
                    print(f"{slug}: not installed")
                else:
                    print(f"{slug}: {info.get('state')} version {info.get('version')}")
            elif cmd == "datasets":
                try:
                    for d in ha.thread_datasets():
                        print(d)
                except HaError as e:
                    print(f"thread datasets unavailable ({e.code}): thread integration probably not loaded")
            else:
                print("usage: python -m tools.ha info|serial|find-xiao|addon-info [slug]|datasets", file=sys.stderr)
                return 2
    except HaError as e:
        print(f"HA error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"cannot reach Home Assistant: {type(e).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
