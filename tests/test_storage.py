from concurrent.futures import ThreadPoolExecutor

import pytest

from jobengine.infrastructure.storage import BudgetExceeded, Store


def test_budget_survives_restarts_and_cannot_be_overcommitted(tmp_path):
    store = Store(tmp_path, budget=1)
    store.reserve("test", "synthetic", 0.6)
    restarted = Store(tmp_path, budget=1)
    with pytest.raises(BudgetExceeded):
        restarted.reserve("test", "synthetic", 0.5)
    assert restarted.usage()["total"] == 0.6


def test_concurrent_budget_reservations_are_atomic(tmp_path):
    store = Store(tmp_path, budget=1)

    def reserve(_):
        try:
            store.reserve("test", "synthetic", 0.4)
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(4))) == 2
    assert store.usage()["total"] == 0.8


def test_cache_and_settlement(tmp_path):
    store = Store(tmp_path)
    receipt = store.reserve("test", "synthetic", 0.5)
    store.settle(receipt, 0.01, 100, 20, 0)
    assert store.usage()["total"] == 0.01
    key = store.key({"profile": "synthetic"})
    store.cache(key, {"result": "cached"})
    assert store.cached(key)["result"] == "cached"
