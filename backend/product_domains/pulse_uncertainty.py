"""Customer-cluster Bootstrap for observed rental-loss severity, not risk scores.

One observation is the total recorded repair/write-off cost for a rental order.
All orders belonging to one customer travel together in each resample. There
is no inference about incident probability, future demand or optimal pricing.
"""
from typing import Any

import numpy as np


def deposit_uncertainty(loss_rows: list[dict[str, Any]],
                        deposits: tuple[int, ...] = (5_000, 10_000), *,
                        truncated: bool = False) -> dict[str, Any]:
    result = {
        "status": "insufficient_data", "method": "customer-cluster percentile Bootstrap",
        "confidence_level": 0.95, "resamples": 2000, "seed": 2026,
        "minimum_customers": 20, "minimum_loss_orders": 30,
        "loss_orders": len(loss_rows), "customers": 0, "scenarios": [],
        "interpretation": (
            "按客户整组重采样，估计已记录损失订单的押金覆盖率与单笔未覆盖损失的95%置信区间；"
            "至少20位客户、30笔损失订单是产品计算门槛，不保证统计充分性。"
            "假设客户间近似独立且所选期间可比；区间不包含漏记、选择偏差或未来环境变化，"
            "不是损失发生概率、单笔损失预测区间或自动押金建议。"
        ),
    }
    if truncated or len(loss_rows) > 10_000:
        return {**result, "status": "data_limit", "message": "记录超过计算上限，未对截断样本推断；请缩短查询期间。"}
    groups: dict[str, list[int]] = {}
    seen_orders: set[str] = set()
    for row in loss_rows:
        customer, order = row.get("customer_id"), row.get("order_id")
        amount = row.get("amount_cents")
        if (not customer or not order or str(order) in seen_orders
                or isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0):
            return {**result, "status": "invalid_source", "message": "损失记录缺少客户、订单或有效金额，不能可靠重采样。"}
        seen_orders.add(str(order))
        groups.setdefault(str(customer), []).append(amount)
    result["customers"] = len(groups)
    if len(groups) > 2000:
        return {**result, "status": "data_limit", "message": "客户超过计算上限，未抽取便利样本；请缩短查询期间。"}
    if len(groups) < 20 or len(loss_rows) < 30:
        return {**result, "message": "真实损失样本不足，仅展示已记录覆盖金额，不生成置信区间。"}
    values = [amount for amounts in groups.values() for amount in amounts]
    if len(set(values)) < 2:
        return {**result, "status": "no_variation", "message": "已记录损失金额没有差异，不用零宽区间暗示确定性。"}

    counts = np.asarray([len(amounts) for amounts in groups.values()], dtype=float)
    losses = np.asarray([sum(amounts) for amounts in groups.values()], dtype=float)
    coverage = np.asarray([[sum(min(value, deposit) for value in amounts)
                            for amounts in groups.values()] for deposit in deposits], dtype=float)
    # Each Bootstrap replicate draws C customers with replacement and retains
    # their complete order histories. Ratios use the resampled denominators.
    rng = np.random.default_rng(result["seed"])
    ratios, residual_means = [], []
    for start in range(0, result["resamples"], 64):
        indices = rng.integers(0, len(groups), size=(min(64, result["resamples"] - start), len(groups)))
        total_loss = losses[indices].sum(axis=1)
        order_counts = counts[indices].sum(axis=1)
        covered = coverage[:, indices].sum(axis=2)
        ratios.append(covered / total_loss)
        residual_means.append((total_loss - covered) / order_counts)
    ratio_samples = np.concatenate(ratios, axis=1)
    residual_samples = np.concatenate(residual_means, axis=1)
    for index, deposit in enumerate(deposits):
        ratio_ci = np.quantile(ratio_samples[index], [0.025, 0.975])
        mean_ci = np.quantile(residual_samples[index], [0.025, 0.975])
        result["scenarios"].append({
            "deposit_cents": deposit,
            "coverage_ratio_ci95": [round(float(value), 4) for value in ratio_ci],
            "mean_uncovered_per_loss_order_cents_ci95": [round(float(value), 2) for value in mean_ci],
        })
    return {**result, "status": "ok"}
