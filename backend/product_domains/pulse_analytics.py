"""Statistical helpers for Pulse BI/EDA.

The input rows are always bounded, user-scoped aggregates from the relational
repository (or explicitly isolated demo data). This module never persists
personal data or substitutes synthetic observations for real-mode data.
"""

from __future__ import annotations

from datetime import datetime
from statistics import mean, median
from typing import Any

import numpy as np


def duckdb_olap_rollup(payment_rows: list[dict[str, Any]], order_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Run an isolated OLAP pass over already user/date-scoped OLTP facts."""
    import duckdb

    connection = duckdb.connect(":memory:", config={"threads": "1", "memory_limit": "128MB"})
    try:
        connection.execute("CREATE TABLE payment_facts(occurred_at VARCHAR, payment_type VARCHAR, amount_cents BIGINT, order_id VARCHAR)")
        connection.execute("CREATE TABLE order_facts(total_cents BIGINT)")
        if payment_rows:
            connection.executemany("INSERT INTO payment_facts VALUES (?, ?, ?, ?)", [
                (str(row["occurred_at"]), str(row["payment_type"]), int(row["amount_cents"]),
                 str(row["order_id"]) if row.get("order_id") is not None else None)
                for row in payment_rows
            ])
        if order_rows:
            connection.executemany("INSERT INTO order_facts VALUES (?)", [(int(row["total_cents"]),) for row in order_rows])
        monthly = connection.execute("""SELECT strftime(try_cast(occurred_at AS TIMESTAMPTZ), '%Y-%m') AS period,
            SUM(CASE WHEN payment_type='rental' THEN amount_cents ELSE 0 END) AS revenue,
            SUM(CASE WHEN payment_type IN ('rental','deposit') THEN amount_cents ELSE 0 END) AS cash_in,
            SUM(CASE WHEN payment_type='deposit_refund' THEN amount_cents ELSE 0 END) AS cash_out,
            COUNT(DISTINCT CASE WHEN payment_type='rental' THEN order_id END) AS orders
            FROM payment_facts WHERE try_cast(occurred_at AS TIMESTAMPTZ) IS NOT NULL
            GROUP BY 1 ORDER BY 1""").fetchall()
        order_values = [int(row[0]) for row in connection.execute(
            "SELECT total_cents FROM order_facts ORDER BY total_cents").fetchall()]
        return {"monthly_rows": [{"period": row[0], "revenue": int(row[1] or 0),
                                  "cash_in": int(row[2] or 0), "cash_out": int(row[3] or 0),
                                  "orders": int(row[4] or 0)} for row in monthly],
                "order_values": order_values,
                "engine": "DuckDB in-memory OLAP over user/date-scoped relational OLTP facts",
                "source_payment_rows": len(payment_rows), "source_order_rows": len(order_rows)}
    finally:
        connection.close()


def distribution(values: list[float]) -> dict[str, Any]:
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return {"count": 0, "mean": None, "median": None, "q1": None,
                "q3": None, "min": None, "max": None, "outliers": []}
    q1, median_value, q3 = (float(np.percentile(clean, point, method="linear"))
                            for point in (25, 50, 75))
    iqr = q3 - q1
    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return {
        "count": len(clean), "mean": round(mean(clean), 2),
        "median": round(median_value, 2), "q1": round(q1, 2),
        "q3": round(q3, 2), "min": round(clean[0], 2),
        "max": round(clean[-1], 2),
        "whisker_low": round(next((value for value in clean if value >= lower), clean[0]), 2),
        "whisker_high": round(next((value for value in reversed(clean) if value <= upper), clean[-1]), 2),
        "outliers": [round(value, 2) for value in clean if value < lower or value > upper],
    }


def pca_projection(rows: list[dict[str, Any]], feature_names: list[str]) -> dict[str, Any]:
    usable = [row for row in rows if all(_finite_number(row.get(name)) for name in feature_names)]
    if len(usable) < 3 or len(feature_names) < 2:
        return {"status": "insufficient_data", "minimum_rows": 3,
                "observations": len(usable), "feature_names": feature_names,
                "points": [], "components": [], "explained_variance_ratio": []}

    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    matrix = np.asarray([[float(row[name]) for name in feature_names] for row in usable], dtype=float)
    scaler = StandardScaler()
    standardized = scaler.fit_transform(matrix)
    distinct_rows = int(np.unique(standardized, axis=0).shape[0])
    if distinct_rows < 2 or not np.isfinite(standardized).all():
        return {"status": "no_feature_variation", "minimum_rows": 3,
                "observations": len(usable), "feature_names": feature_names,
                "points": [], "components": [], "explained_variance_ratio": [],
                "message": "有效特征没有足够差异，无法生成有意义的 PCA 投影或聚类。"}
    component_count = min(2, len(feature_names), len(usable) - 1)
    model = PCA(n_components=component_count, svd_solver="full")
    coordinates = model.fit_transform(standardized)
    cluster_count = min(4, max(2, round((len(usable) / 2) ** 0.5)), distinct_rows)
    labels = KMeans(n_clusters=cluster_count, random_state=2026, n_init=10).fit_predict(standardized)
    components = []
    for component_index, weights in enumerate(model.components_, start=1):
        ranked = sorted(zip(feature_names, weights.tolist()), key=lambda item: abs(item[1]), reverse=True)
        components.append({"name": f"PC{component_index}",
                           "loadings": [{"feature": name, "weight": round(float(weight), 4)}
                                        for name, weight in ranked]})
    return {
        "status": "ok", "method": "StandardScaler + sklearn PCA",
        "segmentation_method": f"KMeans (k={cluster_count}, random_state=2026)",
        "observations": len(usable), "feature_names": feature_names,
        "explained_variance_ratio": [round(float(value), 4) for value in model.explained_variance_ratio_],
        "components": components,
        "clusters": [{"cluster": int(cluster), "count": int(np.sum(labels == cluster))}
                     for cluster in range(cluster_count)],
        "points": [{"id": str(row.get("id", index)),
                    "label": str(row.get("label") or row.get("name") or row.get("id") or f"row {index + 1}"),
                    "cluster": int(labels[index]),
                    "pc1": round(float(coordinates[index, 0]), 4),
                    "pc2": round(float(coordinates[index, 1]), 4) if component_count > 1 else 0.0}
                   for index, row in enumerate(usable)],
    }


def linear_revenue_forecast(monthly_rows: list[dict[str, Any]], horizon: int = 3) -> dict[str, Any]:
    """A transparent univariate trend baseline, not a seasonal/causal forecast."""
    by_month: dict[str, float] = {}
    for row in monthly_rows:
        try:
            month = datetime.strptime(str(row["period"]), "%Y-%m")
            revenue = float(row.get("revenue") or 0)
            if np.isfinite(revenue):
                by_month[month.strftime("%Y-%m")] = revenue
        except (KeyError, TypeError, ValueError):
            continue
    if len(by_month) < 3:
        return {"status": "insufficient_data", "observations": len(by_month), "minimum_months": 3,
                "method": "ordinary least squares linear time trend", "points": []}
    month_numbers = sorted(datetime.strptime(key, "%Y-%m") for key in by_month)
    start, end = month_numbers[0], month_numbers[-1]
    continuous = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        key = f"{year:04d}-{month:02d}"
        continuous.append((key, by_month.get(key, 0.0)))
        year, month = (year + (month == 12), 1 if month == 12 else month + 1)
    full_y = np.asarray([row[1] for row in continuous], dtype=float)
    # Bound the transparent OLS calculation, but keep complete observed history
    # available to suitably data-hungry ML/deep-learning comparisons.
    y = full_y[-60:]
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = intercept + slope * x
    residual = y - fitted
    residual_error = float(np.sqrt(np.sum(residual ** 2) / max(1, len(y) - 2)))
    x_mean = float(np.mean(x))
    sxx = float(np.sum((x - x_mean) ** 2))
    last_date = month_numbers[-1]
    points = []
    for step in range(1, max(1, min(12, int(horizon))) + 1):
        forecast_x = float(len(y) - 1 + step)
        expected = max(0.0, float(intercept + slope * forecast_x))
        prediction_error = residual_error * (1 + 1 / len(y) + ((forecast_x - x_mean) ** 2 / sxx if sxx else 0)) ** 0.5
        target_month = last_date.month - 1 + step
        target_year = last_date.year + target_month // 12
        target_month = target_month % 12 + 1
        points.append({"period": f"{target_year:04d}-{target_month:02d}", "revenue": round(expected, 2),
                       "lower_95": round(max(0.0, expected - 1.96 * prediction_error), 2),
                       "upper_95": round(max(0.0, expected + 1.96 * prediction_error), 2)})
    ml_comparison = compare_revenue_models(full_y, last_date, horizon)
    return {"status": "ok", "observations": len(full_y), "method": "ordinary least squares linear time trend",
            "horizon_months": len(points), "points": points,
            "ml_comparison": ml_comparison,
            "assumptions": ["missing months between first and last observation are treated as zero revenue",
                            "univariate linear trend; no seasonality, pricing, demand or causal variables"]}


def simulated_revenue_demo(months: int = 72, seed: int = 2026, baseline_cents: int = 250_000) -> dict[str, Any]:
    """Reproducible synthetic series for UI/model demonstrations; never actual business data."""
    months = max(36, min(180, int(months)))
    rng = np.random.default_rng(seed)
    start = datetime(2020, 1, 1)
    rows = []
    for index in range(months):
        month_offset = start.month - 1 + index
        year, month = start.year + month_offset // 12, month_offset % 12 + 1
        baseline_cents = max(1, int(baseline_cents))
        seasonal = baseline_cents * 0.18 * np.sin(2 * np.pi * (month - 1) / 12)
        trend = baseline_cents * 0.006 * index
        noise = float(rng.normal(0, baseline_cents * 0.08))
        revenue = max(0, int(baseline_cents + trend + seasonal + noise))
        rows.append({"period": f"{year:04d}-{month:02d}", "revenue": revenue,
                     "cash_in": revenue, "cash_out": 0, "orders": max(1, round(revenue / 47_000))})
    forecast = linear_revenue_forecast(rows)
    return {"synthetic": True, "seed": seed, "observations": len(rows), "baseline_cents": baseline_cents,
            "disclaimer": "固定随机种子的合成演示数据；仅按经营数据中的月收入中位数校准金额量级，不代表真实客户或未来业绩。",
            "monthly_rows": rows, "forecast": forecast}


def deposit_coverage_scenarios(loss_amounts_cents: list[int], deposits_cents: tuple[int, ...] = (5_000, 10_000)) -> dict[str, Any]:
    """Compare loss-severity coverage; deposit size does not change incident probability."""
    observed = [max(0, int(value)) for value in loss_amounts_cents]
    demo_only = not observed
    amounts = observed or [2_500, 5_000, 7_500, 15_000, 30_000]
    scenarios = []
    for deposit in deposits_cents:
        recovered = sum(min(loss, deposit) for loss in amounts)
        total_loss = sum(amounts)
        scenarios.append({"deposit_cents": int(deposit), "observations": len(amounts),
                          "total_loss_cents": total_loss, "covered_cents": recovered,
                          "residual_loss_cents": max(0, total_loss - recovered),
                          "coverage_ratio": round(recovered / total_loss, 4) if total_loss else None,
                          "average_uncovered_per_event_cents": round(max(0, total_loss - recovered) / len(amounts))})
    return {"status": "illustrative_only" if demo_only else "observed_severity_sensitivity",
            "synthetic": demo_only, "scenarios": scenarios,
            "interpretation": "押金只用于比较已记录损失严重度下的可覆盖金额与剩余损失；不改变损坏/遗失/逾期发生概率。演示损失额是假设，不是真实样本或建议押金。"}


def aggregate_order_context(rows: list[dict[str, Any]], minimum_group: int = 5) -> dict[str, Any]:
    """Aggregate operational context against observed outcomes; never individual-score customers."""
    dimensions = ("party_size_group", "planned_sets_group", "discount_pressure", "subjective_urgency",
                  "objective_urgency", "flower_add_on", "deposit_paid", "cancellation_type")
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {name: {} for name in dimensions}
    for row in rows:
        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            starts = datetime.fromisoformat(str(row["start_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None: created = created.replace(tzinfo=timezone.utc)
            if starts.tzinfo is None: starts = starts.replace(tzinfo=timezone.utc)
            lead_hours = max(0.0, (starts - created).total_seconds() / 3600)
            objective = "急迫（≤24小时）" if lead_hours <= 24 else "较急（≤7天）" if lead_hours <= 168 else "常规（>7天）"
        except (KeyError, TypeError, ValueError):
            objective = "未知"
        party, sets = int(row.get("party_size") or 1), int(row.get("planned_sets") or 1)
        categories = {
            "party_size_group": "1人" if party == 1 else "2–3人" if party <= 3 else "4人以上",
            "planned_sets_group": "1套" if sets == 1 else "2–3套" if sets <= 3 else "4套以上",
            "discount_pressure": str(row.get("discount_pressure") or "unknown"),
            "subjective_urgency": str(row.get("subjective_urgency") or "unknown"),
            "objective_urgency": objective,
            "flower_add_on": "有小熊手工花加购" if row.get("flower_add_on") else "无/未记录小熊手工花加购",
            "deposit_paid": "已收定金" if row.get("deposit_paid") else "未收定金/未记录",
            "cancellation_type": ("临时取消" if row.get("cancellation_reason") == "last_minute_customer_cancel"
                                  else "未按约到场" if row.get("cancellation_reason") == "no_show"
                                  else "其他取消" if row.get("status") == "cancelled"
                                  else "未取消/未记录"),
        }
        for dimension, category in categories.items():
            grouped[dimension].setdefault(category, []).append(row)

    output = {}
    for dimension, categories in grouped.items():
        visible, suppressed_orders = [], 0
        for category, members in sorted(categories.items(), key=lambda item: (-len(item[1]), item[0])):
            if len(members) < minimum_group:
                suppressed_orders += len(members)
                continue
            def rate(numerator: str, denominator: str) -> float | None:
                base = sum(int(item.get(denominator) or 0) for item in members)
                return round(sum(int(item.get(numerator) or 0) for item in members) / base, 4) if base else None
            visible.append({"category": category, "orders": len(members),
                            "late_return_rate": rate("late_return", "return_observed"),
                            "damage_rate": rate("damaged", "inspected"),
                            "missing_rate": rate("missing", "inspected"),
                            "current_overdue_orders": sum(int(item.get("current_overdue") or 0) for item in members),
                            "cancelled_orders": sum(str(item.get("status")) == "cancelled" for item in members),
                            "last_minute_cancellations": sum(item.get("cancellation_reason") == "last_minute_customer_cancel" for item in members),
                            "no_shows": sum(item.get("cancellation_reason") == "no_show" for item in members),
                            "return_observations": sum(int(item.get("return_observed") or 0) for item in members),
                            "inspected_orders": sum(int(item.get("inspected") or 0) for item in members)})
        output[dimension] = {"groups": visible, "suppressed_orders": suppressed_orders}
    return {"dimensions": output, "minimum_group_size": minimum_group,
            "policy": "Observed group-level rates only; small groups suppressed. Subjective urgency is customer-stated; objective urgency is order lead time. No individual risk score or causal claim."}


def _revenue_features(values: list[float], target_index: int) -> list[float]:
    recent = [values[target_index - lag] if target_index >= lag else 0.0 for lag in (1, 2, 3)]
    recent_mean = float(np.mean(recent))
    month_angle = 2 * np.pi * (target_index % 12) / 12
    return [*recent, recent_mean, float(np.sin(month_angle)), float(np.cos(month_angle)), values[target_index - 12]]


def _pytorch_lstm_forecast(values: np.ndarray, last_date: datetime, horizon: int,
                           minimum_months: int = 120) -> dict[str, Any]:
    """Optional CPU LSTM: only run with a long monthly series and chronological holdout."""
    if len(values) < minimum_months:
        return {"status": "insufficient_history", "minimum_months": minimum_months,
                "observations": len(values), "model": "pytorch_lstm", "points": []}
    try:
        import torch
        from torch import nn
    except ImportError:
        return {"status": "unavailable", "minimum_months": minimum_months,
                "observations": len(values), "model": "pytorch_lstm", "points": [],
                "reason": "install optional backend/requirements-ml.txt"}

    from sklearn.metrics import mean_absolute_error, mean_squared_error

    torch.manual_seed(2026)
    previous_torch_threads = torch.get_num_threads()
    torch.set_num_threads(1)
    lookback = 12
    holdout = max(12, int(np.ceil((len(values) - lookback) * 0.2)))
    split = len(values) - holdout

    class RevenueLSTM(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(input_size=1, hidden_size=8, batch_first=True)
            self.output = nn.Linear(8, 1)

        def forward(self, batch):
            sequence, _ = self.lstm(batch)
            return self.output(sequence[:, -1, :])

    def train_model(train_values: np.ndarray):
        center = float(np.mean(train_values))
        scale = max(float(np.std(train_values)), 1.0)
        normalized = (train_values - center) / scale
        x = np.asarray([normalized[index - lookback:index] for index in range(lookback, len(normalized))], dtype=np.float32)
        y = np.asarray([normalized[index] for index in range(lookback, len(normalized))], dtype=np.float32)
        model = RevenueLSTM()
        optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
        loss_fn = nn.MSELoss()
        x_tensor = torch.from_numpy(x).unsqueeze(-1)
        y_tensor = torch.from_numpy(y).unsqueeze(-1)
        model.train()
        for _ in range(60):
            optimizer.zero_grad()
            loss = loss_fn(model(x_tensor), y_tensor)
            loss.backward()
            optimizer.step()
        return model.eval(), center, scale

    validation_model, center, scale = train_model(values[:split])
    validation_windows = np.asarray([(values[index - lookback:index] - center) / scale
                                     for index in range(split, len(values))], dtype=np.float32)
    with torch.no_grad():
        validation_prediction = validation_model(torch.from_numpy(validation_windows).unsqueeze(-1)).squeeze(-1).numpy() * scale + center
    actual = values[split:]
    evaluation = {"model": "pytorch_lstm", "mae": round(float(mean_absolute_error(actual, np.maximum(0, validation_prediction))), 2),
                  "rmse": round(float(mean_squared_error(actual, np.maximum(0, validation_prediction)) ** 0.5), 2),
                  "holdout_months": len(actual), "validation": "chronological rolling one-step holdout"}

    model, center, scale = train_model(values)
    history = ((values - center) / scale).astype(np.float32).tolist()
    points = []
    for step in range(1, max(1, min(12, int(horizon))) + 1):
        window = torch.tensor(history[-lookback:], dtype=torch.float32).view(1, lookback, 1)
        with torch.no_grad():
            normalized_prediction = float(model(window).item())
        prediction = max(0.0, normalized_prediction * scale + center)
        history.append((prediction - center) / scale)
        month_index = last_date.month - 1 + step
        year, month = last_date.year + month_index // 12, month_index % 12 + 1
        points.append({"period": f"{year:04d}-{month:02d}", "revenue": round(prediction, 2)})
    torch.set_num_threads(previous_torch_threads)
    return {"status": "ok", "minimum_months": minimum_months, "observations": len(values),
            "evaluation": evaluation, "points": points,
            "method": "PyTorch LSTM(12个月窗口，8个隐藏单元，CPU，60轮训练)"}


def compare_revenue_models(values: np.ndarray, last_date: datetime, horizon: int = 3) -> dict[str, Any]:
    """Compare ML forecasts only after enough real monthly history exists.

    Backtests preserve time order and reserve at least one full year for
    evaluation. A last-value baseline is included so a complex model is not
    selected unless it improves on a transparent naive forecast.
    """
    values = np.asarray(values, dtype=float)
    minimum_months = 60
    if len(values) < minimum_months:
        return {"status": "insufficient_history", "minimum_months": minimum_months,
                "observations": len(values), "models": [], "selected_model": None, "points": []}
    from sklearn.ensemble import AdaBoostRegressor, RandomForestRegressor
    from sklearn.linear_model import BayesianRidge
    from sklearn.metrics import mean_absolute_error, mean_squared_error
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeRegressor

    indexes = list(range(12, len(values)))
    features = np.asarray([_revenue_features(values.tolist(), index) for index in indexes])
    targets = values[indexes]
    holdout = max(12, int(np.ceil(len(indexes) * 0.2)))
    split = len(indexes) - holdout
    if split < 12:
        return {"status": "insufficient_history", "minimum_months": minimum_months,
                "observations": len(values), "models": [], "selected_model": None, "points": []}
    candidates = {
        "random_forest": RandomForestRegressor(n_estimators=160, max_depth=4, min_samples_leaf=2,
                                                random_state=2026, n_jobs=1),
        "adaboost": AdaBoostRegressor(estimator=DecisionTreeRegressor(max_depth=2), n_estimators=100,
                                       learning_rate=0.05, random_state=2026),
        "bayesian_ridge": make_pipeline(StandardScaler(), BayesianRidge()),
    }
    actual = targets[split:]
    baseline_prediction = features[split:, 0]
    evaluations = [{"model": "last_value_baseline",
                    "mae": round(float(mean_absolute_error(actual, baseline_prediction)), 2),
                    "rmse": round(float(mean_squared_error(actual, baseline_prediction) ** 0.5), 2),
                    "holdout_months": len(actual),
                    "validation": "chronological rolling one-step holdout"}]
    for name, estimator in candidates.items():
        estimator.fit(features[:split], targets[:split])
        predicted = np.maximum(0.0, estimator.predict(features[split:]))
        evaluations.append({"model": name, "mae": round(float(mean_absolute_error(actual, predicted)), 2),
                            "rmse": round(float(mean_squared_error(actual, predicted) ** 0.5), 2),
                            "holdout_months": len(actual), "validation": "chronological rolling one-step holdout"})
    deep_learning = _pytorch_lstm_forecast(values, last_date, horizon)
    if deep_learning.get("status") == "ok":
        evaluations.append(deep_learning["evaluation"])
    best_model = min(evaluations, key=lambda item: (
        item["mae"], item["model"] != "last_value_baseline", item["model"],
    ))
    baseline_mae = evaluations[0]["mae"]
    # Small holdout wins may just be noise; prefer the transparent baseline
    # unless a candidate clears a modest practical improvement threshold.
    selected_name = (best_model["model"] if best_model["model"] == "last_value_baseline"
                     or best_model["mae"] <= baseline_mae * 0.95
                     else "last_value_baseline")
    if selected_name not in {"pytorch_lstm", "last_value_baseline"}:
        selected = candidates[selected_name]
        selected.fit(features, targets)
    history = values.tolist()
    last_month_index = len(history) - 1
    forecast_points = []
    for step in range(1, max(1, min(12, int(horizon))) + 1):
        target_index = last_month_index + step
        if selected_name == "pytorch_lstm":
            prediction = deep_learning["points"][step - 1]["revenue"]
        elif selected_name == "last_value_baseline":
            prediction = history[-1]
        else:
            prediction = max(0.0, float(selected.predict(np.asarray([_revenue_features(history, target_index)]))[0]))
        history.append(prediction)
        month_index = last_date.month - 1 + step
        year = last_date.year + month_index // 12
        month = month_index % 12 + 1
        forecast_points.append({"period": f"{year:04d}-{month:02d}", "revenue": round(prediction, 2)})
    return {"status": "ok", "minimum_months": minimum_months, "observations": len(values),
            "validation": "chronological rolling one-step holdout; no shuffled split",
            "selection_rule": "候选模型 MAE 至少比上月值基线低 5% 才选用，否则保留基线",
            "selected_model": selected_name, "models": evaluations, "points": forecast_points,
            "deep_learning": deep_learning}


def _finite_number(value: Any) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def build_eda(*, monthly_rows: list[dict[str, Any]], order_values: list[float],
              feature_rows: list[dict[str, Any]], feature_names: list[str]) -> dict[str, Any]:
    return {
        "methodology": "关系型数据库OLTP → 用户/日期范围ETL → DuckDB内存OLAP；押金不作为收入；描述统计仅针对所选期间可用记录。",
        "monthly_trends": monthly_rows,
        "revenue_forecast": linear_revenue_forecast(monthly_rows),
        "distributions": {"order_value_cents": distribution(order_values),
                          "monthly_revenue_cents": distribution([row["revenue"] for row in monthly_rows])},
        "pca": pca_projection(feature_rows, feature_names),
    }
