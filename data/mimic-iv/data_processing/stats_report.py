"""统计报告: (A) 当前抽取数据集的 ICD-9/ICD-10 占比与画像; (B) MIMIC-IV 综合数据概况。

只产出聚合数字(无 PHI), 报告写入 docs/mimic-iv/数据统计报告.md。

用法:
  python stats_report.py                # 含大表全量计数(labevents 等, 较慢)
  python stats_report.py --fast         # 跳过 labevents/notes 的全表计数
"""

from __future__ import annotations

import argparse
import collections
import json
import time
from pathlib import Path

import duckdb

from build_dataset import DEFAULT_DATA_ROOT, csv, module_paths


def _pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.1f}%" if total else "—"


# --------------------------------------------------------------------------- #
# Part A: 当前数据集画像(读 dataset.jsonl)
# --------------------------------------------------------------------------- #
def report_current_dataset(jsonl: Path) -> list[str]:
    md = ["## A. 当前抽取数据集画像\n"]
    if not jsonl.exists():
        md.append(f"_未找到 {jsonl}, 跳过。_\n")
        return md
    rows = [json.loads(l) for l in jsonl.open(encoding="utf-8")]
    n = len(rows)
    md.append(f"- 数据文件: `{jsonl}`")
    md.append(f"- 病例总数: **{n}**\n")

    # ICD 版本占比
    ver = collections.Counter(r["diagnosis_hidden"].get("icd_version") for r in rows)
    md.append("### A.1 主诊断 ICD 版本占比(当前样本)\n")
    md.append("| ICD 版本 | 病例数 | 占比 |\n|---|---|---|")
    for v in sorted(ver, key=lambda x: (x is None, x)):
        md.append(f"| ICD-{v} | {ver[v]} | {_pct(ver[v], n)} |")
    md.append("")

    # 性别
    g = collections.Counter(r["demographics"].get("gender") for r in rows)
    md.append("### A.2 性别 / 年龄\n")
    md.append("| 性别 | 数量 | 占比 |\n|---|---|---|")
    for k in sorted(g, key=lambda x: (x is None, x)):
        md.append(f"| {k} | {g[k]} | {_pct(g[k], n)} |")
    ages = [r["demographics"].get("age") for r in rows if r["demographics"].get("age") is not None]
    if ages:
        ages_sorted = sorted(ages)
        md.append(
            f"\n- 年龄: min {min(ages)} / 中位 {ages_sorted[len(ages)//2]} / max {max(ages)} "
            f"(注: ≥89 岁被 MIMIC 聚合)\n"
        )
    buckets = collections.Counter()
    for a in ages:
        buckets[f"{(a//10)*10}-{(a//10)*10+9}"] += 1
    md.append("| 年龄段 | 数量 |\n|---|---|")
    for b in sorted(buckets):
        md.append(f"| {b} | {buckets[b]} |")
    md.append("")

    # 检查覆盖
    def cov(fn):
        return sum(1 for r in rows if fn(r))
    md.append("### A.3 检查 / 病史覆盖\n")
    md.append("| 字段 | 有数据病例 | 占比 |\n|---|---|---|")
    cov_items = [
        ("HPI 现病史", lambda r: bool(r["history"].get("hpi"))),
        ("既往史 PMH", lambda r: bool(r["history"].get("pmh"))),
        ("入院体格检查", lambda r: bool(r["exams"].get("physical_exam_admission"))),
        ("实验室化验", lambda r: bool(r["exams"].get("labs"))),
        ("影像检查", lambda r: bool(r["exams"].get("radiology"))),
        ("微生物检查", lambda r: bool(r["exams"].get("microbiology"))),
        ("出院诊断文本", lambda r: bool(r["diagnosis_hidden"].get("discharge_diagnosis_text"))),
    ]
    for name, fn in cov_items:
        c = cov(fn)
        md.append(f"| {name} | {c} | {_pct(c, n)} |")
    labn = [len(r["exams"].get("labs", [])) for r in rows]
    if labn:
        labn_sorted = sorted(labn)
        md.append(
            f"\n- 每例化验项数: min {min(labn)} / 中位 {labn_sorted[len(labn)//2]} / max {max(labn)}\n"
        )

    # Top 主诊断
    titles = collections.Counter(r["diagnosis_hidden"].get("title") for r in rows)
    md.append("### A.4 Top 10 主诊断\n")
    md.append("| 主诊断 | 病例数 |\n|---|---|")
    for title, c in titles.most_common(10):
        md.append(f"| {title} | {c} |")
    md.append("")
    return md


# --------------------------------------------------------------------------- #
# Part B: MIMIC-IV 综合数据概况(DuckDB 直查 csv.gz)
# --------------------------------------------------------------------------- #
def report_mimic_overview(p: dict, fast: bool) -> list[str]:
    con = duckdb.connect()
    con.execute("PRAGMA threads=4")

    def scalar(sql: str):
        return con.execute(sql).fetchone()[0]

    md = ["## B. MIMIC-IV 综合数据概况\n"]

    # --- 表级行数(小/中表) ---
    md.append("### B.1 关键表行数 / 实体规模\n")
    md.append("| 表 | 行数 | 说明 |\n|---|---|---|")

    t0 = time.time()
    n_pat = scalar(f"SELECT COUNT(*) FROM {csv(p['patients'])}")
    md.append(f"| patients | {n_pat:,} | 病人数(subject_id) |")

    n_adm = scalar(f"SELECT COUNT(*) FROM {csv(p['admissions'])}")
    n_adm_subj = scalar(f"SELECT COUNT(DISTINCT subject_id) FROM {csv(p['admissions'])}")
    md.append(f"| admissions | {n_adm:,} | 住院次数; 涉及 {n_adm_subj:,} 名病人 |")

    n_ed = scalar(f"SELECT COUNT(*) FROM {csv(p['edstays'])}")
    n_ed_adm = scalar(
        f"SELECT COUNT(*) FROM {csv(p['edstays'])} WHERE hadm_id IS NOT NULL"
    )
    md.append(f"| ed/edstays | {n_ed:,} | 急诊就诊; 其中 {n_ed_adm:,} 次收入住院 |")

    n_dx = scalar(f"SELECT COUNT(*) FROM {csv(p['diagnoses_icd'])}")
    md.append(f"| diagnoses_icd | {n_dx:,} | 诊断记录(含所有 seq_num) |")
    print(f"[stats] 小表计数完成 {time.time()-t0:.1f}s", flush=True)

    if not fast:
        t1 = time.time()
        n_note = scalar(f"SELECT COUNT(*) FROM {csv(p['discharge'])}")
        md.append(f"| note/discharge | {n_note:,} | 出院小结(自由文本) |")
        n_rad = scalar(f"SELECT COUNT(*) FROM {csv(p['radiology'])}")
        md.append(f"| note/radiology | {n_rad:,} | 影像报告(自由文本) |")
        print(f"[stats] note 计数完成 {time.time()-t1:.1f}s", flush=True)

        t2 = time.time()
        n_lab = scalar(f"SELECT COUNT(*) FROM {csv(p['labevents'])}")
        md.append(f"| labevents | {n_lab:,} | 化验记录(最大表) |")
        print(f"[stats] labevents 计数完成 {time.time()-t2:.1f}s", flush=True)
    else:
        md.append("| note/discharge, radiology, labevents | _(--fast 跳过)_ | 全表计数较慢 |")
    md.append("")

    # --- 性别 ---
    md.append("### B.2 病人性别分布\n")
    g = con.execute(
        f"SELECT gender, COUNT(*) c FROM {csv(p['patients'])} GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    md.append("| 性别 | 数量 | 占比 |\n|---|---|---|")
    for gv, c in g:
        md.append(f"| {gv} | {c:,} | {_pct(c, n_pat)} |")
    md.append("")

    # --- 全库主诊断 ICD-9/10 占比(最关键) ---
    md.append("### B.3 全库主诊断(seq_num=1)的 ICD-9 / ICD-10 占比\n")
    rows = con.execute(
        f"""
        SELECT CAST(icd_version AS INTEGER) v, COUNT(*) c
        FROM {csv(p['diagnoses_icd'])}
        WHERE seq_num = '1'
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    tot = sum(c for _, c in rows)
    md.append("| ICD 版本 | 主诊断数 | 占比 |\n|---|---|---|")
    for v, c in rows:
        md.append(f"| ICD-{v} | {c:,} | {_pct(c, tot)} |")
    md.append(f"| 合计 | {tot:,} | 100% |\n")

    # --- 各版本不同诊断码数量 ---
    rows2 = con.execute(
        f"""
        SELECT CAST(icd_version AS INTEGER) v, COUNT(DISTINCT icd_code) c
        FROM {csv(p['diagnoses_icd'])}
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    md.append("### B.4 各 ICD 版本的不同诊断码数\n")
    md.append("| ICD 版本 | 不同诊断码数 |\n|---|---|")
    for v, c in rows2:
        md.append(f"| ICD-{v} | {c:,} |")
    md.append("")

    # --- admission_type 分布 ---
    md.append("### B.5 入院类型分布\n")
    at = con.execute(
        f"SELECT admission_type, COUNT(*) c FROM {csv(p['admissions'])} GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    md.append("| 入院类型 | 数量 | 占比 |\n|---|---|---|")
    for a, c in at:
        md.append(f"| {a} | {c:,} | {_pct(c, n_adm)} |")
    md.append("")

    con.close()
    return md


def main(argv=None) -> int:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--jsonl", default=str(here / "output" / "dataset.jsonl"))
    ap.add_argument(
        "--out",
        default=str(here.parents[2] / "docs" / "mimic-iv" / "数据统计报告.md"),
        help="报告输出路径",
    )
    ap.add_argument("--fast", action="store_true", help="跳过大表全表计数")
    args = ap.parse_args(argv)

    paths = module_paths(args.data_root)
    md: list[str] = []
    md.append("# MIMIC-IV 数据统计报告\n")
    md.append(
        f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} · "
        f"数据: MIMIC-IV v3.1 (hosp) + ED v2.2 + Note v2.2\n"
    )
    md.append(
        "> 说明: 本报告仅含聚合统计, 不含任何可识别的患者记录。\n"
    )

    md += report_current_dataset(Path(args.jsonl))
    md += report_mimic_overview(paths, args.fast)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"[done] 统计报告 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
