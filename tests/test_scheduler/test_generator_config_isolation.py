"""The sequence generator's cached config never leaks between tests
(tests/conftest.py::_generator_config_fresh). The two tests run in file
order: the first leaves a gate-on config in the cache the old way, the
second must not see it."""
from photonscript.scheduler import nina_sequence_json as nsj


def test_a_gate_on_config_is_cached_inside_one_test(monkeypatch):
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    assert nsj._gen_cfg_cache is None
    assert nsj._gen_cfg().cooler_gate_mode == "skip"
    assert nsj._gen_cfg_cache is not None


def test_the_next_test_starts_with_no_cached_config():
    assert nsj._gen_cfg_cache is None
    assert nsj._gen_cfg().cooler_gate_mode == "off"
