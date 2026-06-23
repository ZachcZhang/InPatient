"""把 build_dataset.py 产出的 case JSON 渲染成人类可读的 Markdown 病历(plain 版)。

支持中英双语(--lang zh/en)。每个 case → 一个 .md, 内容包含:
  - 患者基本信息 / 主诉与分诊
  - 病历(现病史、既往史、社会史、家族史、过敏、入院用药等)
  - 各项检查(**出院前/诊断期**: 入院体格检查、实验室、影像、微生物)
  - 最终诊断(隐藏答案, 默认折叠在末尾, 可用 --hide-answer 完全去掉)

用法:
  python export_markdown.py                       # 中文 → output/markdown/*.md
  python export_markdown.py --lang en             # 英文 → output/markdown/en/*.md
  python export_markdown.py --combined            # 额外合并成一个 all_cases.md
  python export_markdown.py --hide-answer         # 不输出隐藏诊断(纯问诊素材)
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import List, Optional

# Pertinent Results 原文里"出院时化验"小节的起始标记(整块剔除, 出院检查除外)。
_DISCHARGE_LAB_BLOCK = re.compile(
    r"(?i)^\s*(?:labs?\s+(?:on|at|upon)\s+discharge|discharge\s+labs?|on\s+discharge)\b"
)

HISTORY_ORDER = [
    "chief_complaint", "hpi", "ros", "pmh", "psh",
    "social_history", "family_history", "allergies", "medications_on_admission",
]

# 所有 UI 文案的双语映射
TXT = {
    "zh": {
        "history_titles": {
            "chief_complaint": "主诉 (Chief Complaint)",
            "hpi": "现病史 (HPI)",
            "ros": "系统回顾 (ROS)",
            "pmh": "既往史 (PMH)",
            "psh": "手术史 (PSH)",
            "social_history": "个人/社会史 (Social History)",
            "family_history": "家族史 (Family History)",
            "allergies": "过敏史 (Allergies)",
            "medications_on_admission": "入院用药 (Medications on Admission)",
        },
        "title": "病例",
        "source": "数据来源",
        "note": (
            "说明: 下列检查均为 **出院前 / 诊断期** 数据(出院当天检查已剔除; "
            "诊断期窗口 {win}h)。文本中的 `___` 为 MIMIC 去标识占位符。"
        ),
        "sec_demo": "## 一、基本信息",
        "tbl_demo_head": "| 项目 | 值 |\n|---|---|",
        "age": "年龄", "gender": "性别", "race": "种族",
        "marital": "婚姻", "language": "语言",
        "sec_pres": "## 二、主诉与分诊(入院时医生可见)",
        "cc": "主诉", "adm_type": "入院类型", "transport": "转运方式",
        "acuity": "急诊分级 (acuity)", "vitals": "分诊生命体征",
        "sec_hist": "## 三、病历(患者可提供的病史)",
        "sec_pe": "## 四、入院体格检查(出院查体已剔除)",
        "sec_labs": "## 五、实验室检查(诊断期)",
        "labs_head": "| 检查项 | 标本 | 结果 | 参考范围 | 异常 |\n|---|---|---|---|---|",
        "no_labs": "_无诊断期化验记录_",
        "pert_summary": "出院小结中的 Pertinent Results 原文(出院化验已剔除)",
        "sec_rad": "## 六、影像检查(诊断期)",
        "no_rad": "_无诊断期影像记录_",
        "image": "影像",
        "sec_micro": "## 七、微生物检查(诊断期)",
        "micro_head": "| 时间 | 标本 | 检测 | 病原体 | 药敏 | 解读 |\n|---|---|---|---|---|---|",
        "no_micro": "_无诊断期微生物记录_",
        "sec_dx": "## 八、最终诊断(隐藏答案 — 仅供评测, 请勿提供给问诊模型)",
        "dx_summary": "点击展开金标准诊断",
        "primary_icd": "主诊断 ICD",
        "dc_dx": "出院诊断原文",
        "ed_dx": "急诊诊断",
    },
    "en": {
        "history_titles": {
            "chief_complaint": "Chief Complaint",
            "hpi": "History of Present Illness (HPI)",
            "ros": "Review of Systems (ROS)",
            "pmh": "Past Medical History (PMH)",
            "psh": "Past Surgical History (PSH)",
            "social_history": "Social History",
            "family_history": "Family History",
            "allergies": "Allergies",
            "medications_on_admission": "Medications on Admission",
        },
        "title": "Case",
        "source": "Source",
        "note": (
            "Note: all examinations below are **pre-discharge / diagnostic-window** data "
            "(discharge-day exams removed; diagnostic window {win}h). "
            "`___` are MIMIC de-identification placeholders."
        ),
        "sec_demo": "## 1. Demographics",
        "tbl_demo_head": "| Field | Value |\n|---|---|",
        "age": "Age", "gender": "Sex", "race": "Race",
        "marital": "Marital status", "language": "Language",
        "sec_pres": "## 2. Chief Complaint & Triage (visible to doctor at intake)",
        "cc": "Chief complaint", "adm_type": "Admission type", "transport": "Arrival transport",
        "acuity": "Acuity", "vitals": "Triage vitals",
        "sec_hist": "## 3. History (patient-reportable)",
        "sec_pe": "## 4. Admission Physical Exam (discharge exam removed)",
        "sec_labs": "## 5. Laboratory Tests (diagnostic window)",
        "labs_head": "| Test | Specimen | Result | Reference range | Abnormal |\n|---|---|---|---|---|",
        "no_labs": "_No diagnostic-window lab records_",
        "pert_summary": "Pertinent Results text from discharge summary (discharge labs removed)",
        "sec_rad": "## 6. Imaging (diagnostic window)",
        "no_rad": "_No diagnostic-window imaging records_",
        "image": "Image",
        "sec_micro": "## 7. Microbiology (diagnostic window)",
        "micro_head": "| Time | Specimen | Test | Organism | Antibiotic | Interpretation |\n|---|---|---|---|---|---|",
        "no_micro": "_No diagnostic-window microbiology records_",
        "sec_dx": "## 8. Final Diagnosis (HIDDEN ANSWER — for evaluation only, do NOT expose to the interviewing model)",
        "dx_summary": "Click to reveal gold-standard diagnosis",
        "primary_icd": "Primary ICD",
        "dc_dx": "Discharge diagnosis text",
        "ed_dx": "ED diagnoses",
    },
}


def _fmt(v, dash: str = "—") -> str:
    if v is None or v == "":
        return dash
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _strip_discharge_lab_blocks(text: Optional[str]) -> Optional[str]:
    """从 Pertinent Results 原文中剔除"出院时化验"小节(出院检查除外)。"""
    if not text:
        return text
    blocks = re.split(r"\n\s*\n", text)
    kept = []
    for b in blocks:
        first = next((ln for ln in b.splitlines() if ln.strip()), "")
        if _DISCHARGE_LAB_BLOCK.match(first):
            continue
        kept.append(b)
    return "\n\n".join(kept).strip()


def _blockquote(text: Optional[str]) -> str:
    if not text:
        return "> —\n"
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines) + "\n"


def _vitals_line(v: dict) -> str:
    parts = [
        ("T", v.get("temperature")), ("HR", v.get("heartrate")),
        ("RR", v.get("resprate")), ("SpO2", v.get("o2sat")),
        ("SBP", v.get("sbp")), ("DBP", v.get("dbp")), ("Pain", v.get("pain")),
    ]
    return " · ".join(f"{k} {_fmt(val)}" for k, val in parts if val not in (None, ""))


def _labs_table(labs: List[dict], t: dict) -> str:
    if not labs:
        return t["no_labs"] + "\n"
    out = [t["labs_head"]]
    labs = sorted(
        labs,
        key=lambda x: (x.get("category") or "", x.get("charttime") or "", x.get("label") or ""),
    )
    for l in labs:
        # A2: 优先用 value_display(已用 valuenum 回填去标识的 ___ ); 回退 value/valuenum
        disp = l.get("value_display")
        if disp is None or str(disp).strip() == "":
            disp = l.get("value")
        if disp is None or str(disp).strip().strip("_") == "":
            disp = l.get("valuenum")
        result = f"{_fmt(disp)} {l.get('valueuom') or ''}".strip()
        lo, hi = l.get("ref_range_lower"), l.get("ref_range_upper")
        ref = f"{_fmt(lo)} – {_fmt(hi)}" if (lo is not None or hi is not None) else "—"
        flag = l.get("flag")
        is_abn = bool(flag) and str(flag).lower() not in ("", "none")
        mark = f"**⚠ {flag}**" if is_abn else ""
        label = l.get("label") or f"itemid {l.get('itemid')}"
        if is_abn:
            label, result = f"**{label}**", f"**{result}**"
        out.append(f"| {label} | {l.get('fluid') or '—'} | {result} | {ref} | {mark} |")
    return "\n".join(out) + "\n"


def _radiology_block(rads: List[dict], t: dict) -> str:
    if not rads:
        return t["no_rad"] + "\n"
    chunks = []
    for i, r in enumerate(rads, 1):
        head = f"**{t['image']} {i} · {r.get('note_type') or 'RAD'}**"
        if r.get("charttime"):
            head += f" · {r['charttime']}"
        chunks.append(head + "\n\n" + _blockquote(r.get("text")))
    return "\n".join(chunks)


def _micro_table(micros: List[dict], t: dict) -> str:
    if not micros:
        return t["no_micro"] + "\n"
    out = [t["micro_head"]]
    for m in micros:
        out.append(
            "| {t} | {spec} | {test} | {org} | {ab} | {interp} |".format(
                t=_fmt(m.get("charttime")), spec=_fmt(m.get("spec_type_desc")),
                test=_fmt(m.get("test_name")), org=_fmt(m.get("org_name")),
                ab=_fmt(m.get("ab_name")), interp=_fmt(m.get("interpretation")),
            )
        )
    return "\n".join(out) + "\n"


def render_case(case: dict, include_answer: bool = True, lang: str = "zh") -> str:
    t = TXT[lang]
    htitles = t["history_titles"]
    src = case.get("source", {})
    demo = case.get("demographics", {})
    pres = case.get("presentation", {})
    hist = case.get("history", {})
    exams = case.get("exams", {})
    dx = case.get("diagnosis_hidden", {})
    prov = case.get("provenance", {})

    md: List[str] = []
    md.append(f"# {t['title']} {case.get('case_id')}\n")
    md.append(
        f"> {t['source']}: {src.get('dataset', 'MIMIC-IV')} · "
        f"subject_id `{src.get('subject_id')}` · hadm_id `{src.get('hadm_id')}`\n>\n"
        f"> {t['note'].format(win=prov.get('lab_window_hours'))}\n"
    )

    md.append(t["sec_demo"] + "\n")
    md.append(t["tbl_demo_head"])
    md.append(f"| {t['age']} | {_fmt(demo.get('age'))} |")
    md.append(f"| {t['gender']} | {_fmt(demo.get('gender'))} |")
    md.append(f"| {t['race']} | {_fmt(demo.get('race'))} |")
    md.append(f"| {t['marital']} | {_fmt(demo.get('marital_status'))} |")
    md.append(f"| {t['language']} | {_fmt(demo.get('language'))} |\n")

    md.append(t["sec_pres"] + "\n")
    md.append(f"- **{t['cc']}**: {_fmt(pres.get('chief_complaint'))}")
    md.append(f"- **{t['adm_type']}**: {_fmt(pres.get('admission_type'))}")
    md.append(f"- **{t['transport']}**: {_fmt(pres.get('arrival_transport'))}")
    md.append(f"- **{t['acuity']}**: {_fmt(pres.get('acuity'))}")
    md.append(f"- **{t['vitals']}**: {_vitals_line(pres.get('triage_vitals', {})) or '—'}\n")

    md.append(t["sec_hist"] + "\n")
    for key in HISTORY_ORDER:
        if key == "chief_complaint":
            continue
        if hist.get(key):
            md.append(f"### {htitles[key]}\n")
            md.append(_blockquote(hist[key]))

    md.append(t["sec_pe"] + "\n")
    md.append(_blockquote(exams.get("physical_exam_admission")))

    md.append(t["sec_labs"] + "\n")
    md.append(_labs_table(exams.get("labs", []), t))
    pert = _strip_discharge_lab_blocks(exams.get("pertinent_results_text"))
    if pert:
        md.append(f"\n<details><summary>{t['pert_summary']}</summary>\n")
        md.append(_blockquote(pert))
        md.append("</details>\n")

    md.append(t["sec_rad"] + "\n")
    md.append(_radiology_block(exams.get("radiology", []), t))

    md.append(t["sec_micro"] + "\n")
    md.append(_micro_table(exams.get("microbiology", []), t))

    if include_answer:
        md.append(t["sec_dx"] + "\n")
        md.append(f"<details><summary>{t['dx_summary']}</summary>\n")
        md.append(
            f"- **{t['primary_icd']}**: `{_fmt(dx.get('primary_icd'))}` "
            f"(ICD-{_fmt(dx.get('icd_version'))}) — {_fmt(dx.get('title'))}\n"
        )
        if dx.get("discharge_diagnosis_text"):
            md.append(f"- **{t['dc_dx']}**:\n")
            md.append(_blockquote(dx["discharge_diagnosis_text"]))
        ed = dx.get("ed_diagnosis") or []
        if ed:
            md.append(
                f"- **{t['ed_dx']}**: "
                + "; ".join(f"{e.get('icd_title')} (`{e.get('icd_code')}`)" for e in ed)
                + "\n"
            )
        md.append("</details>\n")

    return "\n".join(md).rstrip() + "\n"


def main(argv=None) -> int:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", default=str(here / "output" / "dataset.jsonl"))
    ap.add_argument("--out", default=None, help="输出目录(默认 zh→markdown, en→markdown/en)")
    ap.add_argument("--lang", choices=["zh", "en"], default="zh", help="语言")
    ap.add_argument("--hide-answer", action="store_true", help="不输出隐藏诊断")
    ap.add_argument("--combined", action="store_true", help="额外合并成 all_cases.md")
    ap.add_argument("--limit", type=int, default=0, help="只导出前 N 例(0=全部)")
    args = ap.parse_args(argv)

    jsonl = Path(args.jsonl)
    if not jsonl.exists():
        print(f"[ERROR] 找不到 {jsonl}, 请先运行 build_dataset.py")
        return 1

    if args.out:
        out_dir = Path(args.out)
    else:
        base = here / "output" / "markdown"
        out_dir = base / "en" if args.lang == "en" else base
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [json.loads(line) for line in jsonl.open(encoding="utf-8")]
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    combined_parts: List[str] = []
    for case in rows:
        md = render_case(case, include_answer=not args.hide_answer, lang=args.lang)
        (out_dir / f"{case['case_id']}.md").write_text(md, encoding="utf-8")
        if args.combined:
            combined_parts.append(md)

    if args.combined:
        (out_dir / "all_cases.md").write_text(
            "\n\n---\n\n".join(combined_parts), encoding="utf-8"
        )

    print(f"[done] 已导出 {len(rows)} 份 [{args.lang}] Markdown 病历 → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
