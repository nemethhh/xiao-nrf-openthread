"""Shared fixtures for the Home Assistant stage. Read-only unless a test says otherwise."""
import pytest

from tools.ha import XIAO_HA_DEVICE, HaClient, HaError


@pytest.fixture
def ha():
    """Connected HaClient; skips (never fails) when HA is unreachable or the XIAO is not on the HA host."""
    try:
        client = HaClient()
    except OSError as e:
        pytest.skip(f"cannot read ha_token: {type(e).__name__}")
    try:
        client.__enter__()
    except (HaError, OSError) as e:
        pytest.skip(f"Home Assistant unreachable: {type(e).__name__}")
    try:
        if XIAO_HA_DEVICE not in client.serial_devices():
            pytest.skip(f"XIAO not on the HA host yet: {XIAO_HA_DEVICE} not in HA serial devices")
        yield client
    finally:
        client.close()

