from __future__ import annotations

import csv
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "sample_preprocessed.csv"
RNG = random.Random(20260724)


GROUPS = [
    {
        "余数编号": "R01",
        "数量下限": 0.0,
        "数量上限": 10.0,
        "规格下限": 0.0,
        "规格上限": 20.0,
        "定额单位": "数量",
        "系统值": 2.8,
        "子组": [("类型A", 85, 0.55), ("类型B", 52, 1.25), ("类型C", 18, 0.85)],
    },
    {
        "余数编号": "R01",
        "数量下限": 10.01,
        "数量上限": 30.0,
        "规格下限": 0.0,
        "规格上限": 20.0,
        "定额单位": "数量",
        "系统值": 4.5,
        "子组": [("类型A", 70, 1.20), ("类型B", 41, 2.20), ("类型C", 22, 1.75)],
    },
    {
        "余数编号": "R02",
        "数量下限": 30.01,
        "数量上限": 80.0,
        "规格下限": 20.01,
        "规格上限": 50.0,
        "定额单位": "百分比",
        "系统值": 4.2,
        "子组": [("类型A", 65, 1.40), ("类型B", 38, 2.70), ("类型C", 16, 2.10)],
    },
    {
        "余数编号": "R03",
        "数量下限": 80.01,
        "数量上限": 200.0,
        "规格下限": 0.0,
        "规格上限": 99999.0,
        "定额单位": "百分比",
        "系统值": 2.0,
        "子组": [("类型A", 58, 2.20), ("类型B", 34, 3.10), ("类型C", 14, 2.65)],
    },
]


def bounded_gauss(mean: float, sigma: float) -> float:
    return max(0.0, RNG.gauss(mean, sigma))


def build_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    sample_no = 1
    for group in GROUPS:
        for plating, count, loss_center in group["子组"]:
            for index in range(count):
                plan_qty = RNG.uniform(group["数量下限"] + 0.02, group["数量上限"])
                reported_qty = RNG.uniform(max(1.0, plan_qty * 0.65), max(2.0, plan_qty * 1.10))
                if group["定额单位"] == "百分比":
                    loss_rate = bounded_gauss(loss_center, max(0.25, loss_center * 0.35))
                    actual_loss = plan_qty * loss_rate / 100.0
                    system_remainder = plan_qty * group["系统值"] / 100.0
                else:
                    actual_loss = bounded_gauss(loss_center, max(0.20, loss_center * 0.45))
                    system_remainder = group["系统值"]

                # 少量异常样本用于演示滚镀损耗率>40%的诊断前剔除。
                if index == 0 and plating == "类型C":
                    actual_loss = plan_qty * 0.45

                inbound_qty = reported_qty + actual_loss
                rows.append(
                    {
                        "样本ID": f"S{sample_no:05d}",
                        "余数编号": group["余数编号"],
                        "数量下限": group["数量下限"],
                        "数量上限": group["数量上限"],
                        "规格下限": group["规格下限"],
                        "规格上限": group["规格上限"],
                        "定额单位": group["定额单位"],
                        "镀种大类": plating,
                        "余数": round(system_remainder, 6),
                        "领进数量": round(inbound_qty, 6),
                        "报工数量": round(reported_qty, 6),
                        "计划数量(不含余数)": round(plan_qty, 6),
                        "实际损耗": round(actual_loss, 6),
                        "是否进入分析": "是",
                    }
                )
                sample_no += 1
    return rows


def main() -> None:
    rows = build_rows()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"sample_rows={len(rows)}")
    print(f"output={OUTPUT}")


if __name__ == "__main__":
    main()

