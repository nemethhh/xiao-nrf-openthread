"""Session guards in tests/conftest.py."""
import types

import pytest

import tests.conftest as conftest


def fake_config(showlocals: bool):
    return types.SimpleNamespace(getoption=lambda name, default=None: showlocals if name == "showlocals" else default)


def test_showlocals_is_refused():
    """Frame locals can hold Home Assistant's Thread network key (e.g. the shell's echo of `dataset set active`)."""
    with pytest.raises(pytest.UsageError, match="showlocals"):
        conftest.pytest_configure(fake_config(True))


def test_plain_run_is_allowed():
    conftest.pytest_configure(fake_config(False))
