"""tests/ha/test_xiao52_router.py factory-resets the PERMANENT nRF52840 router: opt-in only, re-provisioned after."""
import pytest

import tests.ha.test_xiao52_router as mod


def drive(gen):
    """Run a generator fixture body the way pytest does: setup, then teardown."""
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)


def test_module_skips_without_opt_in():
    gen = mod._opt_in_then_reprovision({}, [], lambda: pytest.fail("must not re-provision"))
    with pytest.raises(pytest.skip.Exception, match=mod.OPT_IN):
        next(gen)


def test_module_skips_unless_opt_in_is_exactly_1():
    gen = mod._opt_in_then_reprovision({mod.OPT_IN: "yes"}, [], lambda: None)
    with pytest.raises(pytest.skip.Exception):
        next(gen)


def test_reprovisions_after_the_module_when_the_board_was_touched():
    calls = []
    drive(mod._opt_in_then_reprovision({mod.OPT_IN: "1"}, ["xiao52"], lambda: calls.append(1)))
    assert calls == [1]


def test_no_reprovision_when_no_test_touched_the_board():
    calls = []
    drive(mod._opt_in_then_reprovision({mod.OPT_IN: "1"}, [], lambda: calls.append(1)))
    assert calls == []


def test_reprovision_failure_warns_with_the_manual_command():
    def boom():
        raise RuntimeError("`ot dataset set active` timed out")

    with pytest.warns(UserWarning, match="python -m tools.ha_join xiao52 --router"):
        drive(mod._opt_in_then_reprovision({mod.OPT_IN: "1"}, ["xiao52"], boom))


def test_reprovision_joins_as_router_and_closes_the_port():
    events = []

    class Ha:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            events.append("ha closed")

    class Cli:
        def close(self):
            events.append("cli closed")

    def run(board, ha, cli, **kw):
        events.append((board, kw))
        return 0

    mod.reprovision(make_ha=Ha, make_cli=lambda port: Cli(), run=run)
    assert events == [("xiao52", {"router": True}), "cli closed", "ha closed"]
