"""IAA 一致性计算 —— 读两名标注者填好的 CSV, 算 Cohen's κ(及与机器的一致性)。

配套 export_iaa.py。零依赖(不需 sklearn)。计算:
  - 标注者间(A vs B)各维度 κ: 病例级 h_conversable / h_history_patient_ok / h_solvable /
    h_gold_ok / h_decision / h_reasoning(序数, 二次加权 κ), 句子级 h_source(5 类)。
  - 人机一致性(每位标注者 vs 机器): h_decision vs machine_decision, h_solvable vs b2_solvability,
    h_source vs machine_source —— 直接回应"LLM judge 判得准不准"的审稿质疑。

只填了一名标注者也能跑(只出人机一致性)。结果打印并写 output/iaa/iaa_agreement.md。

用法:
  python score_iaa.py --cases-a annoA_cases.csv --cases-b annoB_cases.csv \\
                      --sent-a annoA_sent.csv --sent-b annoB_sent.csv
  python score_iaa.py --cases-a annoA_cases.csv          # 仅人机一致性
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---- 取值规范化 ---------------------------------------------------------- #
_YES = {"y", "yes", "true", "1", "admit-ok"}
_NO = {"n", "no", "false", "0"}
_DECISIONS = {"admit", "review", "reject"}
_SOURCES = {"patient", "collateral", "chart_review", "clinician_observed", "osh_records"}


def norm_bool(v: str) -> Optional[str]:
    s = (v or "").strip().lower()
    if s in _YES:
        return "Y"
    if s in _NO:
        return "N"
    return None


def norm_decision(v: str) -> Optional[str]:
    s = (v or "").strip().lower()
    return s if s in _DECISIONS else None


def norm_source(v: str) -> Optional[str]:
    s = (v or "").strip().lower()
    return s if s in _SOURCES else None


def norm_int05(v: str) -> Optional[int]:
    try:
        n = int(round(float(v)))
        return n if 0 <= n <= 5 else None
    except (TypeError, ValueError):
        return None


# ---- Cohen's κ ----------------------------------------------------------- #
def cohen_kappa(pairs: List[Tuple], weighted: bool = False) -> Tuple[float, float, int]:
    """返回 (kappa, observed_agreement, n)。weighted=True 用二次加权(序数)。"""
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    n = len(pairs)
    if n == 0:
        return float("nan"), float("nan"), 0
    labels = sorted({x for p in pairs for x in p}, key=lambda z: (isinstance(z, str), z))
    idx = {lab: i for i, lab in enumerate(labels)}
    k = len(labels)
    O = [[0.0] * k for _ in range(k)]
    for a, b in pairs:
        O[idx[a]][idx[b]] += 1
    for i in range(k):
        for j in range(k):
            O[i][j] /= n
    row = [sum(O[i]) for i in range(k)]
    col = [sum(O[i][j] for i in range(k)) for j in range(k)]

    if weighted and all(isinstance(lab, (int, float)) for lab in labels) and k > 1:
        def w(i, j):
            return ((labels[i] - labels[j]) / (labels[-1] - labels[0])) ** 2
        num = sum(w(i, j) * O[i][j] for i in range(k) for j in range(k))
        den = sum(w(i, j) * row[i] * col[j] for i in range(k) for j in range(k))
        kappa = 1 - num / den if den else float("nan")
        po = sum((1 - w(i, j)) * O[i][j] for i in range(k) for j in range(k))
        return kappa, po, n

    po = sum(O[i][i] for i in range(k))
    pe = sum(row[i] * col[i] for i in range(k))
    kappa = (po - pe) / (1 - pe) if pe != 1 else float("nan")
    return kappa, po, n


def interp(kappa: float) -> str:
    if kappa != kappa:  # nan
        return "n/a"
    if kappa < 0:
        return "poor"
    if kappa < 0.2:
        return "slight"
    if kappa < 0.4:
        return "fair"
    if kappa < 0.6:
        return "moderate"
    if kappa < 0.8:
        return "substantial"
    return "almost perfect"


# ---- CSV 读取(按前缀匹配列名, 兼容 (Y/N) 等后缀) ------------------------ #
def load_csv(path: Path, key_cols: List[str]) -> Dict[Tuple, dict]:
    rows: Dict[Tuple, dict] = {}
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = tuple(r.get(c, "") for c in key_cols)
            rows[key] = r
    return rows


def col(row: dict, prefix: str) -> str:
    for k, v in row.items():
        if k and k.startswith(prefix):
            return v
    return ""


CASE_DIMS = [
    ("h_conversable", "h_conversable", norm_bool, False),
    ("h_history_patient_ok", "h_history_patient_ok", norm_bool, False),
    ("h_solvable", "h_solvable", norm_bool, False),
    ("h_gold_ok", "h_gold_ok", norm_bool, False),
    ("h_decision", "h_decision", norm_decision, False),
    ("h_reasoning (weighted)", "h_reasoning", norm_int05, True),
]


def pair_dim(a_rows, b_rows, prefix, fn):
    pairs = []
    for key in a_rows.keys() & b_rows.keys():
        pairs.append((fn(col(a_rows[key], prefix)), fn(col(b_rows[key], prefix))))
    return pairs


def human_vs_machine(rows, h_prefix, m_prefix, fn):
    pairs = []
    for r in rows.values():
        pairs.append((fn(col(r, h_prefix)), fn(col(r, m_prefix))))
    return pairs


def main(argv=None) -> int:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases-a", required=True)
    ap.add_argument("--cases-b", default=None)
    ap.add_argument("--sent-a", default=None)
    ap.add_argument("--sent-b", default=None)
    ap.add_argument("--out", default=str(here / "output" / "iaa" / "iaa_agreement.md"))
    args = ap.parse_args(argv)

    lines: List[str] = ["# IAA 一致性报告\n"]

    ca = load_csv(Path(args.cases_a), ["case_id"])
    cb = load_csv(Path(args.cases_b), ["case_id"]) if args.cases_b else None

    if cb:
        lines.append("## 标注者间一致性(A vs B, 病例级)\n")
        lines.append("| 维度 | κ | 解释 | 观测一致率 | n |\n|---|---|---|---|---|")
        for name, prefix, fn, weighted in CASE_DIMS:
            k, po, n = cohen_kappa(pair_dim(ca, cb, prefix, fn), weighted=weighted)
            lines.append(f"| {name} | {k:.3f} | {interp(k)} | {po:.2%} | {n} |")
        lines.append("")

    # 人机一致性(每位标注者 vs 机器)
    lines.append("## 人机一致性(标注者 vs 机器)\n")
    lines.append("| 标注者 | 维度 | κ | 解释 | n |\n|---|---|---|---|---|")
    hm = [
        ("h_decision", "machine_decision", norm_decision),
        ("h_solvable", "b2_solvability", norm_bool),
    ]
    for tag, rows in [("A", ca)] + ([("B", cb)] if cb else []):
        for h_prefix, m_prefix, fn in hm:
            k, _, n = cohen_kappa(human_vs_machine(rows, h_prefix, m_prefix, fn))
            lines.append(f"| {tag} | {h_prefix} vs {m_prefix} | {k:.3f} | {interp(k)} | {n} |")
    lines.append("")

    # 句子级 source
    sa = load_csv(Path(args.sent_a), ["case_id", "sent_id"]) if args.sent_a else None
    sb = load_csv(Path(args.sent_b), ["case_id", "sent_id"]) if args.sent_b else None
    if sa and sb:
        k, po, n = cohen_kappa(pair_dim(sa, sb, "h_source", norm_source))
        lines.append("## 句子级来源归属(A vs B)\n")
        lines.append(f"- h_source κ = **{k:.3f}** ({interp(k)}), 观测一致率 {po:.2%}, n={n}\n")
    if sa:
        k, _, n = cohen_kappa(human_vs_machine(sa, "h_source", "machine_source", norm_source))
        lines.append(f"- A vs 机器 source κ = {k:.3f} ({interp(k)}), n={n}\n")

    report = "\n".join(lines)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[done] → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
