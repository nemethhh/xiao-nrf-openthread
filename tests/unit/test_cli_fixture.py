"""The board fixtures factory-reset before AND after each test (tests/conftest.py `_cli`).

After the Home Assistant e2e test the DK would otherwise keep HA's real network key in flash and
autostart onto the home network on every boot.
"""

import pytest

import tests.conftest as conftest


class FakeCli:
    def __init__(self, port, fail_reset_on: int | None = None):
        self.port = port
        self.resets = 0
        self.closed = False
        self.fail_reset_on = fail_reset_on

    def factory_reset(self):
        self.resets += 1
        if self.resets == self.fail_reset_on:
            raise TimeoutError("node did not come back after factoryreset")

    def close(self):
        self.closed = True


def run_fixture(monkeypatch, **kw) -> FakeCli:
    made = []
    monkeypatch.setattr(conftest, "OtCli", lambda port: made.append(FakeCli(port, **kw)) or made[-1])
    gen = conftest._cli("dk")
    cli = next(gen)
    assert cli.resets == 1  # before the test
    with pytest.raises(StopIteration):
        next(gen)  # teardown, as pytest drives it
    return cli


def test_cli_factory_resets_after_test(monkeypatch):
    cli = run_fixture(monkeypatch)
    assert cli.resets == 2
    assert cli.closed


def test_cli_teardown_reset_failure_only_warns(monkeypatch):
    with pytest.warns(UserWarning, match="factory reset after the test failed"):
        cli = run_fixture(monkeypatch, fail_reset_on=2)
    assert cli.closed
