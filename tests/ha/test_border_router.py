"""Tasks 8-9: the XIAO RCP is visible to HA and the OTBR add-on runs on it. Read-only except the restart test."""
import os
import time

import pytest

from tools.fw import RCP_BAUD
from tools.ha import OTBR_SLUG, XIAO_HA_DEVICE

pytestmark = pytest.mark.ha


def _is_otbr(ds: dict) -> bool:
    src = str(ds.get("source", "")).lower()
    return any(m in src for m in ("otbr", "openthread", "open thread", "border router"))


def test_xiao_visible_on_ha(ha):
    assert XIAO_HA_DEVICE in ha.serial_devices()
    assert "Seeed" in XIAO_HA_DEVICE and "CMSIS-DAP" in XIAO_HA_DEVICE


def test_otbr_addon_running_with_xiao(ha):
    info = ha.addon_info(OTBR_SLUG)
    assert info is not None, "OTBR add-on not installed"
    assert info["state"] == "started"
    assert info["options"]["device"] == XIAO_HA_DEVICE
    assert info["options"]["baudrate"] == str(RCP_BAUD)
    assert info["options"]["flow_control"] is True  # Task 6 ruling: False drops DTR and the XIAO bridge goes silent


def test_thread_dataset_preferred(ha):
    preferred = [d for d in ha.thread_datasets() if d.get("preferred") is True]
    assert len(preferred) == 1
    assert _is_otbr(preferred[0]), f"preferred dataset source is {preferred[0].get('source')!r}"


def test_otbr_survives_addon_restart(ha):
    if os.environ.get("HA_ALLOW_RESTART") != "1":
        pytest.skip("restart_addon is a Home Assistant write; set HA_ALLOW_RESTART=1 after the user agrees")
    ds = next(d for d in ha.thread_datasets() if d.get("preferred") is True)
    before = ha.thread_dataset_tlv(ds["dataset_id"])
    ha.restart_addon(OTBR_SLUG)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        info = ha.addon_info(OTBR_SLUG)
        if info and info["state"] == "started":
            break
        time.sleep(3)
    else:
        pytest.fail("add-on not started within 90 s after restart")
    ds_after = next(d for d in ha.thread_datasets() if d.get("preferred") is True)
    same = ha.thread_dataset_tlv(ds_after["dataset_id"]) == before  # boolean first: never put TLVs in an assert
    assert same, "dataset TLV changed after restart"
