from datetime import datetime

import numpy as np
import pytest

from backend.product_domains.revenue_lstm import RevenueLSTM
from backend.product_domains.revenue_forecast import compare, recursive


def test_lstm_bptt_gradients_match_numerical_derivatives():
    model = RevenueLSTM(hidden=2, seed=42)
    x = np.asarray([[[.1], [.2], [.3]], [[.4], [-.2], [.1]]])
    y = np.asarray([[.5], [-.1]])
    _, grads = model.loss_and_grad(x, y)
    for name, coordinate in [("w", (0, 0)), ("w", (1, 4)), ("w", (2, 6)), ("b", (3,)), ("out", (1, 0)), ("bias", (0,))]:
        old = model.params[name][coordinate]
        model.params[name][coordinate] = old + 1e-5
        upper = model.loss_and_grad(x, y)[0]
        model.params[name][coordinate] = old - 1e-5
        lower = model.loss_and_grad(x, y)[0]
        model.params[name][coordinate] = old
        assert grads[name][coordinate] == pytest.approx((upper - lower) / 2e-5, abs=1e-7)


def test_lstm_learns_and_does_not_need_pytorch():
    x = np.linspace(-1, 1, 36).reshape(12, 3, 1)
    y = x[:, -1, :]
    model = RevenueLSTM(hidden=4)
    initial = model.loss_and_grad(x, y)[0]
    model.fit(x, y)
    assert model.loss_and_grad(x, y)[0] < initial * .2


def test_horizon_validation_and_baseline_selection_are_used_for_real_forecast():
    result = compare([100_000.] * 120, datetime(2026, 9, 1), horizon=3)
    assert result["selected_model"] in {"last_value_baseline", "seasonal_naive"}
    assert result["deep_learning"]["status"] == "ok"
    assert all(len(row["by_horizon"]) == 3 for row in result["models"])
    assert all(point["revenue"] == 100_000 for point in result["points"])
    assert recursive(lambda observed: observed[-1], [10, 20], 3) == [20, 20, 20]
    assert compare([1, float("nan")], datetime(2026, 1, 1))["status"] == "invalid_history"
