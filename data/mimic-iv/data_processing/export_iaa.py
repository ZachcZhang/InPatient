"""IAA 标注导出 —— 把 Stage B 的待复核样本 + B1/B2/B3 结果导成人工标注表。

用途: 为论文效度做人工复核与 inter-annotator agreement(IAA/κ)。产出两张 CSV:
  1) iaa_cases.csv      —— 病例级: 机器分数/标记/决策 + 留空的人工列
  2) iaa_b1_sentences.csv —— 句子级: B1 来源标签 + 留空的人工列(用于来源归属 IAA)

复核队列默认包含: 全部被拒病例 + 全部带 flag_for_review 的病例 + 一批随机"干净通过"病例
(用于平衡, 让 κ 不只在难例上估计)。两名标注者各填一份, 再用 score_iaa(后续)算 κ。

合规: 纯本地读写, 无外部调用。CSV 含 MIMIC 文本, 落在 output/(已 .gitignore), 不得外传。

用法:
  python export_iaa.py                          # 默认队列 + 20 例干净样本
  python export_iaa.py --sample-clean 40 --seed 7
  python export_iaa.py --max-cases 120
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path
from typing import Dict, List, Optional

from stage_b_gates import _sentences

CASE_COLS = [
    # ---- 机器产出(只读, 供标注者参考) ----
    "case_id", "markdown_path", "chief_complaint", "gold_title",
    "b1_patient_ratio", "b2_reasoning", "b2_cc_dx_distance", "b2_gold_consistency",
    "b2_solvability", "b2_recommendation",
    "dx_leak_terms", "tx_leak_n", "machine_flags", "machine_decision", "hpi_excerpt",
    # ---- 人工填写(留空) ----
    "h_conversable(Y/N)", "h_history_patient_ok(Y/N)", "h_solvable(Y/N)",
    "h_reasoning(0-5)", "h_gold_ok(Y/N)", "h_decision(admit/review/reject)", "h_notes",
]
SENT_COLS = [
    "case_id", "sent_id", "sentence", "machine_source", "machine_conf",
    "h_source", "h_notes",
]
README = """# IAA 标注说明

两名标注者各复制一份 `iaa_cases.csv` / `iaa_b1_sentences.csv`, 独立填写 `h_` 开头的列, 切勿互相参考。

## iaa_cases.csv(病例级)
机器列(只读)供参考; 请打开 `markdown_path` 指向的病历后再判断。人工列:
- `h_conversable(Y/N)`        : 患者本人是否可应答问诊(非昏迷/插管/无意识)。
- `h_history_patient_ok(Y/N)` : G_hist 是否基本来自患者本人(无大量旁人/病历代述、无检查发现混入)。
- `h_solvable(Y/N)`           : 仅凭可问到的病史 + 可下单检查, 能否推到 gold 诊断。
- `h_reasoning(0-5)`          : 诊断推理难度(0=主诉即答案 … 5=需多步整合, 见 prompt.py 的 rubric)。
- `h_gold_ok(Y/N)`            : primary/discharge/ED 诊断是否一致、gold 可信。
- `h_decision`                : admit / review / reject(你的最终建议)。
- `h_notes`                   : 备注(尤其泄漏: 病史是否点名诊断/含术后转归)。

## iaa_b1_sentences.csv(句子级, 来源归属)
- `h_source` 从以下选一: patient / collateral / chart_review / clinician_observed / osh_records。
- 标注完成后用一致性脚本计算 Cohen's κ(病例级各维度 + 句子级 source)。
"""


def _flat_leak(qc: dict):
    leak = qc.get("b3_leakage", {}) or {}
    dx = ";".join(h["term"] for h in leak.get("history_dx_leak", []))
    tx = len(leak.get("tx_leak", []))
    return dx, tx


def main(argv=None) -> int:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scores", default=str(here / "output" / "stage_b_scores.jsonl"))
    ap.add_argument("--jsonl", default=str(here / "output" / "dataset.jsonl"))
    ap.add_argument("--out-dir", default=str(here / "output" / "iaa"))
    ap.add_argument("--markdown-dir", default=str(here / "output" / "markdown"))
    ap.add_argument("--sample-clean", type=int, default=20, help="额外抽取的'干净通过'病例数")
    ap.add_argument("--max-cases", type=int, default=0, help="队列上限(0=不限)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    scores_p, jsonl_p = Path(args.scores), Path(args.jsonl)
    if not scores_p.exists() or not jsonl_p.exists():
        print("[ERROR] 找不到 stage_b_scores.jsonl 或 dataset.jsonl, 请先跑 build_dataset.py + stage_b_gates.py")
        return 1

    scores = {json.loads(l)["case_id"]: json.loads(l) for l in scores_p.open(encoding="utf-8")}
    cases = {json.loads(l)["case_id"]: json.loads(l) for l in jsonl_p.open(encoding="utf-8")}

    rng = random.Random(args.seed)
    rejected = [cid for cid, s in scores.items() if not s["admitted"]]
    flagged = [cid for cid, s in scores.items() if s["admitted"] and s.get("flag_for_review")]
    clean = [cid for cid, s in scores.items() if s["admitted"] and not s.get("flag_for_review")]
    rng.shuffle(clean)
    sampled_clean = clean[: max(0, args.sample_clean)]

    queue: List[str] = []
    for cid in rejected + flagged + sampled_clean:
        if cid not in queue:
            queue.append(cid)
    if args.max_cases and len(queue) > args.max_cases:
        queue = queue[: args.max_cases]

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    md_dir = Path(args.markdown_dir)
    n_sent = 0
    with (out / "iaa_cases.csv").open("w", encoding="utf-8", newline="") as cf, \
         (out / "iaa_b1_sentences.csv").open("w", encoding="utf-8", newline="") as sf:
        cw = csv.writer(cf)
        sw = csv.writer(sf)
        cw.writerow(CASE_COLS)
        sw.writerow(SENT_COLS)
        for cid in queue:
            s, c = scores[cid], cases.get(cid, {})
            qc = s
            b2 = qc.get("b2_admission", {}) or {}
            hpi = (c.get("history", {}) or {}).get("hpi") or ""
            dx_leak, tx_n = _flat_leak(qc)
            md_path = md_dir / f"{cid}.md"
            cw.writerow([
                cid,
                str(md_path),
                (c.get("presentation", {}) or {}).get("chief_complaint") or "",
                (c.get("diagnosis_hidden", {}) or {}).get("title") or "",
                qc.get("b1_source", {}).get("source_patient_ratio"),
                b2.get("diagnostic_reasoning_score"),
                b2.get("cc_dx_distance"),
                b2.get("gold_consistency"),
                b2.get("solvability"),
                b2.get("recommendation", ""),
                dx_leak,
                tx_n,
                ";".join(qc.get("flag_for_review", [])),
                "admit" if qc["admitted"] else "reject",
                hpi[:280].replace("\n", " ").strip(),
                "", "", "", "", "", "", "",  # 人工列
            ])
            # 句子级 B1
            sents = _sentences(hpi)
            labels = qc.get("b1_source", {}).get("sentences", [])
            by_id = {}
            for i, it in enumerate(labels):
                by_id[it.get("id", i)] = it
            for i, text in enumerate(sents):
                it = by_id.get(i, {})
                sw.writerow([cid, i, text, it.get("source", ""), it.get("confidence", ""), "", ""])
                n_sent += 1

    (out / "README.md").write_text(README, encoding="utf-8")
    print(f"[iaa] 队列 {len(queue)} 例 (拒绝 {len(rejected)} + 标记 {len(flagged)} + 干净抽样 {len(sampled_clean)})")
    print(f"[iaa] 句子级 {n_sent} 行")
    print(f"[done] → {out/'iaa_cases.csv'} , {out/'iaa_b1_sentences.csv'} , {out/'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
