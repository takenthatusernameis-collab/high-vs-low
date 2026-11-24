from __future__ import annotations

from decimal import Decimal

from high_vs_low.paper_status import _risk_profile


def test_inverse_volatility_path_weights_quieter_leg_more_heavily() -> None:
    history = {}
    for iid, step in ((1, Decimal("0.01")), (2, Decimal("0.04"))):
        price = Decimal("100")
        rows = []
        for day in range(31):
            if day:
                price *= Decimal("1") + (step if day % 2 else -step)
            rows.append({"timestamp_ms": (day + 1) * 86_400_000, "close": str(price)})
        history[iid] = rows
    profile = _risk_profile(1, 2, history, 31 * 86_400_000)
    assert profile["status"] == "RISK_MATCHED_INVERSE_VOLATILITY"
    assert profile["long_weight"] > Decimal("0.5")
    assert profile["short_weight"] < Decimal("0.5")
    assert profile["observations"] == 30


def test_inverse_volatility_falls_back_when_history_is_short() -> None:
    history = {1: [{"timestamp_ms": 86_400_000, "close": "100"}]}
    profile = _risk_profile(1, 2, history, 86_400_000)
    assert profile["status"].startswith("INSUFFICIENT_30_DAY_HISTORY")
    assert profile["long_weight"] == Decimal("0.5")
    assert profile["short_weight"] == Decimal("0.5")
