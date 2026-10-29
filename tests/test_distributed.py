from types import SimpleNamespace

from kkugi_load.distributed import PoolRegistry, rebalancing_problem


def _claim(start, end, pool="p"):
    return {"pool": pool, "start": start, "end": end}


def test_overlapping_range_of_running_worker_is_rejected():
    registry = PoolRegistry()
    assert registry.register("w1", _claim(0, 1000), lambda _: True) is None
    assert registry.register("w2", _claim(1000, 2000), lambda _: True) is None
    reason = registry.register("w3", _claim(999, 1001), lambda _: True)
    assert reason and "w1" in reason
    assert "w3" not in registry.claims


def test_range_of_missing_worker_can_be_reused():
    registry = PoolRegistry()
    registry.register("w1", _claim(0, 1000), lambda _: True)
    assert registry.register("w2", _claim(0, 1000), lambda node: node != "w1") is None


def test_same_worker_can_register_again():
    registry = PoolRegistry()
    registry.register("w1", _claim(0, 1000), lambda _: True)
    assert registry.register("w1", _claim(0, 1000), lambda _: True) is None


def test_master_without_rebalancing_is_rejected():
    reason = rebalancing_problem(SimpleNamespace(enable_rebalancing=False))
    assert reason and "--enable-rebalancing" in reason


def test_master_with_rebalancing_passes():
    assert rebalancing_problem(SimpleNamespace(enable_rebalancing=True)) is None


def test_unknown_options_are_rejected():
    assert rebalancing_problem(None)
    assert rebalancing_problem(SimpleNamespace())
