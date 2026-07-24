from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np


PARENT_COLS = ["余数编号", "数量下限", "数量上限", "规格下限", "规格上限", "定额单位"]
CHILD_COLS = [*PARENT_COLS, "镀种大类"]
NUMERIC_COLS = [
    "数量下限",
    "数量上限",
    "规格下限",
    "规格上限",
    "余数",
    "领进数量",
    "报工数量",
    "计划数量(不含余数)",
    "实际损耗",
]
REQUIRED_COLS = set(CHILD_COLS + NUMERIC_COLS + ["是否进入分析"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="余数放量四维规则诊断")
    parser.add_argument("--input", required=True, type=Path, help="预处理后的订单级CSV")
    parser.add_argument("--output-dir", required=True, type=Path, help="诊断结果目录")
    parser.add_argument("--min-child-n", type=int, default=30, help="子组独立判断的最小样本量")
    parser.add_argument("--max-loss-rate", type=float, default=40.0, help="诊断前剔除的最大实际损耗率")
    return parser.parse_args()


def is_percent(unit: object) -> bool:
    return "百分" in str(unit)


def interval_text(lower: float, upper: float) -> str:
    return f"{lower:g}-{upper:g}"


def metric_value(row: dict[str, object]) -> float:
    return float(row["实际损耗率"] if is_percent(row["定额单位"]) else row["实际损耗"])


def proposed_remainder(row: dict[str, object], proposal: float) -> float:
    if is_percent(row["定额单位"]):
        return float(row["计划数量(不含余数)"]) * proposal / 100.0
    return proposal


def metrics(rows: list[dict[str, object]], proposal: float | None = None) -> dict[str, float]:
    covered = 0
    positive_surplus = 0.0
    for row in rows:
        remainder = float(row["余数"]) if proposal is None else proposed_remainder(row, proposal)
        loss = float(row["实际损耗"])
        covered += remainder >= loss
        positive_surplus += max(remainder - loss, 0.0)
    return {
        "coverage": covered / len(rows),
        "positive_surplus": positive_surplus,
    }


def system_mean(rows: list[dict[str, object]]) -> float:
    if is_percent(rows[0]["定额单位"]):
        values = [
            float(row["余数"]) / float(row["计划数量(不含余数)"]) * 100.0
            for row in rows
        ]
    else:
        values = [float(row["余数"]) for row in rows]
    return float(np.mean(values))


def quantile(rows: list[dict[str, object]], value: float) -> float:
    return float(np.quantile([metric_value(row) for row in rows], value))


def diagnose_direction(rows: list[dict[str, object]]) -> dict[str, object]:
    current = metrics(rows)
    quantiles: dict[str, dict[str, object]] = {}
    for label, level in [("P85", 0.85), ("P90", 0.90), ("P95", 0.95)]:
        value = quantile(rows, level)
        result = metrics(rows, value)
        reduction = current["positive_surplus"] - result["positive_surplus"]
        reduction_rate = reduction / current["positive_surplus"] if current["positive_surplus"] > 0 else 0.0
        quantiles[label] = {
            "value": value,
            "metrics": result,
            "reduction": reduction,
            "reduction_rate": reduction_rate,
        }

    action: tuple[str, str] | None = None
    if current["coverage"] < 0.85 and quantiles["P85"]["metrics"]["coverage"] >= 0.85:
        action = ("上调", "P85")
    elif current["coverage"] >= 0.95 and quantiles["P90"]["reduction_rate"] >= 0.30:
        action = ("下调", "P90")
    elif current["coverage"] >= 0.95 and quantiles["P85"]["reduction_rate"] >= 0.30:
        action = ("下调", "P85")

    if action is None:
        return {"current": current, "quantiles": quantiles, "action": "维持"}
    return {
        "current": current,
        "quantiles": quantiles,
        "action": action[0],
        "basis": action[1],
        "selected": quantiles[action[1]],
    }


def priority_score(
    rows: list[dict[str, object]],
    direction: str,
    current_coverage: float,
    proposed_coverage: float,
    reduction_rate: float,
    inherited: bool,
) -> tuple[float, str]:
    if direction not in {"上调", "下调"}:
        return 0.0, "观察"

    confidence = min(math.log1p(len(rows)) / math.log1p(300), 1.0)
    if direction == "下调":
        effect = min(max(reduction_rate, 0.0) / 0.50, 1.0)
        urgency = min(max(current_coverage - 0.95, 0.0) / 0.05, 1.0)
    else:
        effect = min(max(proposed_coverage - current_coverage, 0.0) / 0.15, 1.0)
        urgency = min(max(0.85 - current_coverage, 0.0) / 0.15, 1.0)

    score = (0.45 * effect + 0.35 * confidence + 0.20 * urgency) * 100
    if inherited:
        score *= 0.45
    score = round(score, 1)
    if score >= 75:
        return score, "A-优先"
    if score >= 55:
        return score, "B-较高"
    if score >= 35:
        return score, "C-一般"
    return score, "D-靠后"


def histogram_payload(rows: list[dict[str, object]]) -> dict[str, object]:
    values = np.asarray([metric_value(row) for row in rows], dtype=float)
    p99 = float(np.quantile(values, 0.99))
    maximum = float(values.max())
    displayed = values[values <= p99]
    excluded = int((values > p99).sum())
    bin_count = min(30, max(12, math.ceil(math.sqrt(len(displayed)))))
    lower = min(0.0, float(displayed.min()))
    upper = p99 if p99 > lower else lower + 1.0
    counts, edges = np.histogram(displayed, bins=bin_count, range=(lower, upper))
    return {
        "displayed": int(len(displayed)),
        "excluded": excluded,
        "p99": p99,
        "max": maximum,
        "edges": [round(float(value), 6) for value in edges],
        "counts": [int(value) for value in counts],
    }


def load_clean_data(path: Path, max_loss_rate: float) -> tuple[list[dict[str, object]], dict[str, int]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(REQUIRED_COLS - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"输入文件缺少字段：{', '.join(missing)}")
        raw = list(reader)

    clean: list[dict[str, object]] = []
    eligible_count = 0
    high_loss_count = 0
    for source_row in raw:
        if str(source_row["是否进入分析"]).strip() != "是":
            continue
        eligible_count += 1
        row: dict[str, object] = dict(source_row)
        try:
            for column in NUMERIC_COLS:
                row[column] = float(str(source_row[column]).replace(",", ""))
        except (TypeError, ValueError):
            continue
        plan_qty = float(row["计划数量(不含余数)"])
        if plan_qty <= 0:
            continue
        row["实际损耗率"] = float(row["实际损耗"]) / plan_qty * 100.0
        if float(row["实际损耗率"]) > max_loss_rate:
            high_loss_count += 1
            continue
        clean.append(row)

    return clean, {
        "原始样本数": len(raw),
        "预处理可分析样本数": eligible_count,
        "损耗率阈值剔除数": high_loss_count,
        "最终可分析样本数": len(clean),
    }


def group_rows(
    rows: list[dict[str, object]],
    columns: list[str],
) -> dict[tuple[object, ...], list[dict[str, object]]]:
    groups: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row[column] for column in columns)].append(row)
    return dict(groups)


def run_diagnosis(
    clean: list[dict[str, object]],
    min_child_n: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    parent_groups = group_rows(clean, PARENT_COLS)
    child_groups = group_rows(clean, CHILD_COLS)
    parent_diagnoses = {key: diagnose_direction(rows) for key, rows in parent_groups.items()}
    records: list[dict[str, object]] = []
    current_total = 0.0
    p90_total = 0.0
    p95_total = 0.0
    p90_covered = 0
    p95_covered = 0

    for key, rows in child_groups.items():
        parent_key = key[:-1]
        parent_rows = parent_groups[parent_key]
        parent_diagnosis = parent_diagnoses[parent_key]
        child_diagnosis = diagnose_direction(rows)
        independent = len(rows) >= min_child_n
        selected = child_diagnosis if independent else parent_diagnosis

        parent_p90 = quantile(parent_rows, 0.90)
        parent_p95 = quantile(parent_rows, 0.95)
        child_p90 = quantile(rows, 0.90)
        child_p95 = quantile(rows, 0.95)
        p90_value = child_p90 if independent else parent_p90
        p95_value = child_p95 if independent else parent_p95
        p90_result = metrics(rows, p90_value)
        p95_result = metrics(rows, p95_value)
        current = child_diagnosis["current"]

        current_total += float(current["positive_surplus"])
        p90_total += float(p90_result["positive_surplus"])
        p95_total += float(p95_result["positive_surplus"])
        p90_covered += round(float(p90_result["coverage"]) * len(rows))
        p95_covered += round(float(p95_result["coverage"]) * len(rows))

        direction = str(selected["action"])
        if direction in {"上调", "下调"}:
            selected_metrics = selected["selected"]["metrics"]
            selected_reduction_rate = float(selected["selected"]["reduction_rate"])
        else:
            selected_metrics = current
            selected_reduction_rate = 0.0
        score, level = priority_score(
            rows,
            direction,
            float(current["coverage"]),
            float(selected_metrics["coverage"]),
            selected_reduction_rate,
            inherited=not independent,
        )

        records.append(
            {
                "余数编号": str(key[0]),
                "数量区间": interval_text(float(key[1]), float(key[2])),
                "规格区间": interval_text(float(key[3]), float(key[4])),
                "定额单位": str(key[5]),
                "镀种大类": str(key[-1]),
                "样本量": len(rows),
                "父组样本量": len(parent_rows),
                "父组系统余数均值": system_mean(parent_rows),
                "父组P90值": parent_p90,
                "父组P95值": parent_p95,
                "分位值来源": "子组" if independent else "父组",
                "当前覆盖率": float(current["coverage"]),
                "当前正向多余余数": float(current["positive_surplus"]),
                "P90值": p90_value,
                "P90覆盖率": float(p90_result["coverage"]),
                "P90预计减少比例": (
                    (float(current["positive_surplus"]) - float(p90_result["positive_surplus"]))
                    / float(current["positive_surplus"])
                    if float(current["positive_surplus"]) > 0
                    else 0.0
                ),
                "P95值": p95_value,
                "P95覆盖率": float(p95_result["coverage"]),
                "P95预计减少比例": (
                    (float(current["positive_surplus"]) - float(p95_result["positive_surplus"]))
                    / float(current["positive_surplus"])
                    if float(current["positive_surplus"]) > 0
                    else 0.0
                ),
                "调整判断": direction,
                "判断来源": "镀种子组独立判断" if independent else "样本不足_继承父组判断",
                "优先级得分": score,
                "优先级": level,
                "指标名称": "实际损耗率（%）" if is_percent(key[5]) else "实际损耗（百粒）",
                **histogram_payload(rows),
            }
        )

    order = {"上调": 0, "下调": 0, "维持": 1}
    records.sort(
        key=lambda row: (
            order[str(row["调整判断"])],
            -float(row["优先级得分"]),
            str(row["余数编号"]),
            str(row["数量区间"]),
            str(row["规格区间"]),
            str(row["镀种大类"]),
        )
    )
    for index, row in enumerate(records, start=1):
        row["图表编号"] = f"N{index:04d}"

    summary = {
        "三维父规则组数": len(parent_groups),
        "四维子规则组数": len(records),
        "独立判断规则数": sum(row["分位值来源"] == "子组" for row in records),
        "父组继承规则数": sum(row["分位值来源"] == "父组" for row in records),
        "当前总体覆盖率": sum(float(row["余数"]) >= float(row["实际损耗"]) for row in clean) / len(clean),
        "P90方案总体覆盖率": p90_covered / len(clean),
        "P95方案总体覆盖率": p95_covered / len(clean),
        "当前总体正向多余余数": current_total,
        "P90方案正向多余余数": p90_total,
        "P95方案正向多余余数": p95_total,
    }
    for prefix in ["P90方案", "P95方案"]:
        reduction = summary["当前总体正向多余余数"] - summary[f"{prefix}正向多余余数"]
        summary[f"{prefix}减少量"] = reduction
        summary[f"{prefix}减少比例"] = (
            reduction / summary["当前总体正向多余余数"]
            if summary["当前总体正向多余余数"] > 0
            else 0.0
        )
    return records, summary


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    clean, data_summary = load_clean_data(args.input, args.max_loss_rate)
    if not clean:
        raise ValueError("没有可用于诊断的样本")
    records, diagnosis_summary = run_diagnosis(clean, args.min_child_n)
    payload = {
        "meta": {
            "实际损耗口径": "领进数量 - 报工数量",
            "四维规则": "余数编号×数量区间×规格区间×镀种大类",
            "子组最小样本量": args.min_child_n,
            "损耗率剔除阈值": args.max_loss_rate,
            "分位值回退": "子组样本量不足时使用三维父组P90/P95",
        },
        "summary": {**data_summary, **diagnosis_summary},
        "rules": records,
    }
    (args.output_dir / "diagnostics.json").write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )

    csv_records = [{key: value for key, value in row.items() if key not in {"edges", "counts"}} for row in records]
    with (args.output_dir / "rule_diagnostics.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_records[0]))
        writer.writeheader()
        writer.writerows(csv_records)
    print(f"analysis_rows={len(clean)}")
    print(f"rules={len(records)}")
    print(f"output={args.output_dir / 'diagnostics.json'}")


if __name__ == "__main__":
    main()

