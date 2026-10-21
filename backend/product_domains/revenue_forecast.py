"""Bounded, cached revenue forecasts evaluated at the actual forecast horizon."""
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from threading import Lock

import numpy as np

from .revenue_lstm import fit_series, next_value

_training_lock = Lock()


def features(history):
    target = len(history)
    recent = [history[-lag] for lag in (1, 2, 3)]
    angle = 2 * np.pi * (target % 12) / 12
    return [*recent, float(np.mean(recent)), float(np.sin(angle)), float(np.cos(angle)), history[-12]]


def recursive(predictor, history, horizon):
    work = list(history)
    result = []
    for _ in range(horizon):
        value = max(0.0, float(predictor(work)))
        if not np.isfinite(value):
            raise ValueError("Forecast is not finite.")
        result.append(value)
        work.append(value)
    return result


def compare(values, last_date: datetime, horizon: int = 3):
    history = tuple(float(value) for value in values)
    horizon = max(1, min(12, int(horizon)))
    if not np.isfinite(history).all() or any(value < 0 for value in history):
        return {"status": "invalid_history", "models": [], "points": [], "selected_model": None}
    # The last 20 years bound training time and cache space on the free server.
    with _training_lock:
        result = _compare(history[-240:], last_date.isoformat(), horizon)
    return deepcopy(result)


@lru_cache(maxsize=8)
def _compare(history: tuple, last_date: str, horizon: int):
    count = len(history)
    if count < 60:
        return {"status": "insufficient_history", "minimum_months": 60,
                "observations": count, "models": [], "selected_model": None, "points": []}
    from sklearn.ensemble import AdaBoostRegressor, RandomForestRegressor
    from sklearn.linear_model import BayesianRidge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeRegressor

    values = np.asarray(history)
    holdout = max(12, int(np.ceil((count - 12) * .2)))
    split = count - holdout
    x = np.asarray([features(history[:index]) for index in range(12, split)])
    y = values[12:split]
    estimators = {
        "random_forest": RandomForestRegressor(n_estimators=80, max_depth=4, min_samples_leaf=2, random_state=2026, n_jobs=1),
        "adaboost": AdaBoostRegressor(estimator=DecisionTreeRegressor(max_depth=2), n_estimators=60, learning_rate=.05, random_state=2026),
        "bayesian_ridge": make_pipeline(StandardScaler(), BayesianRidge()),
    }
    predictors = {"last_value_baseline": lambda observed: observed[-1],
                  "seasonal_naive": lambda observed: observed[-12]}
    for name, estimator in estimators.items():
        estimator.fit(x, y)
        predictors[name] = lambda observed, fitted=estimator: fitted.predict(np.asarray([features(observed)]))[0]
    deep_learning = {"status": "insufficient_history", "minimum_months": 120, "observations": count, "model": "numpy_lstm"}
    if count >= 120:
        fitted_lstm = fit_series(values[:split])
        predictors["numpy_lstm"] = lambda observed: next_value(observed, fitted_lstm)
        deep_learning["status"] = "ok"

    # Fit on the initial training prefix only. At each held-out origin, use
    # only facts known by that origin; steps 2..H consume predictions, not
    # future observed values. Every candidate sees exactly the same targets.
    origins = range(split, count - horizon + 1)
    actual = np.asarray([values[origin:origin+horizon] for origin in origins])
    evaluations = []
    for name, predictor in predictors.items():
        predicted = np.asarray([recursive(predictor, history[:origin], horizon) for origin in origins])
        errors = predicted - actual
        evaluations.append({"model": name, "mae": round(float(np.mean(np.abs(errors))), 2),
                            "rmse": round(float(np.sqrt(np.mean(errors ** 2))), 2),
                            "holdout_months": holdout, "forecast_origins": len(actual),
                            "validation": "chronological rolling-origin recursive multi-step holdout",
                            "by_horizon": [{"months_ahead": step + 1,
                                            "mae": round(float(np.mean(np.abs(errors[:, step]))), 2),
                                            "rmse": round(float(np.sqrt(np.mean(errors[:, step] ** 2))), 2)}
                                           for step in range(horizon)]})
    baseline = min(evaluations[:2], key=lambda row: (row["mae"], row["model"] != "last_value_baseline"))
    candidate = min(evaluations[2:], key=lambda row: (row["mae"], row["model"]))
    selected = candidate if candidate["mae"] < baseline["mae"] and candidate["mae"] <= .95 * baseline["mae"] else baseline
    name = selected["model"]
    if name == "numpy_lstm":
        full_fit = fit_series(values)
        predictor = lambda observed: next_value(observed, full_fit)
    elif name in estimators:
        estimator = estimators[name]
        estimator.fit(np.asarray([features(history[:index]) for index in range(12, count)]), values[12:])
        predictor = lambda observed: estimator.predict(np.asarray([features(observed)]))[0]
    else:
        predictor = predictors[name]
    forecast = recursive(predictor, history, horizon)
    last = datetime.fromisoformat(last_date)
    points = []
    for step, value in enumerate(forecast, 1):
        index = last.month - 1 + step
        points.append({"period": f"{last.year + index // 12:04d}-{index % 12 + 1:02d}", "revenue": round(value, 2)})
    if deep_learning["status"] == "ok":
        deep_learning["evaluation"] = next(row for row in evaluations if row["model"] == "numpy_lstm")
        deep_learning["method"] = "NumPy CPU LSTM; 12-month window; 8 hidden units; BPTT + Adam; 60 epochs"
    return {"status": "ok", "minimum_months": 60, "observations": count,
            "validation": "chronological rolling-origin recursive multi-step holdout; no shuffled split",
            "selection_rule": "与上月值及去年同月中表现更好的基线比较，复杂模型 MAE 至少改善5%才选用",
            "selected_model": name, "baseline_model": baseline["model"],
            "models": evaluations, "points": points, "horizon_months": horizon, "deep_learning": deep_learning}
