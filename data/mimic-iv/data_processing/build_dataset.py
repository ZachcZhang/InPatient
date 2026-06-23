"""从 MIMIC-IV 抽取 patient-simulator 基础数据集。

论文: Patients Are Not Oracles: Evaluating Interactive Clinical Reasoning
      beyond Complete-Information QA

目标(本脚本只做"取数", 不做图谱抽取/verifier):
  把 MIMIC-IV(hosp) + MIMIC-IV-ED + MIMIC-IV-Note 重建成"按时间平面分层"的
  case JSON, 每个 case 含:
    - 患者基本信息(人口学 + 分诊主诉/生命体征)
    - 病史(G_hist, 患者入院时可知)
    - 检查信息(G_exam, **出院检查除外**: 入院查体 + 诊断期化验/影像/微生物)
    - 隐藏答案(G_dx, 主诊断 ICD + Discharge Diagnosis 文本)

引擎: DuckDB 直接流式读取 .csv.gz, 对超大表(labevents 2.5GB 等)用 cohort
半连接下推, 单节点低内存即可跑。

合规: 全程本地处理, 不向任何第三方 API 发送 MIMIC 文本。

用法:
  python build_dataset.py --limit 200                # 先抽 200 例小样本
  python build_dataset.py --limit 0                  # 全量
  python build_dataset.py --stages cohort            # 只跑 cohort(快, 验证用)
"""

from __future__ import annotations

import argparse
import json
import datetime as _dt
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import duckdb

from note_sections import (
    deid_density,
    detect_history_source,
    is_conversable,
    segment_note,
    split_to_planes,
)

# --------------------------------------------------------------------------- #
# 路径配置
# --------------------------------------------------------------------------- #
DEFAULT_DATA_ROOT = "/Volumes/Elements/数据/physionet.org/files/mimiciv"


def module_paths(data_root: str) -> Dict[str, str]:
    hosp = os.path.join(data_root, "mimic-iv", "hosp")
    ed = os.path.join(data_root, "mimic-iv-ed", "2.2", "ed")
    note = os.path.join(data_root, "mimic-iv-note", "2.2", "note")
    return {
        "patients": os.path.join(hosp, "patients.csv.gz"),
        "admissions": os.path.join(hosp, "admissions.csv.gz"),
        "diagnoses_icd": os.path.join(hosp, "diagnoses_icd.csv.gz"),
        "d_icd_diagnoses": os.path.join(hosp, "d_icd_diagnoses.csv.gz"),
        "labevents": os.path.join(hosp, "labevents.csv.gz"),
        "d_labitems": os.path.join(hosp, "d_labitems.csv.gz"),
        "microbiologyevents": os.path.join(hosp, "microbiologyevents.csv.gz"),
        "edstays": os.path.join(ed, "edstays.csv.gz"),
        "triage": os.path.join(ed, "triage.csv.gz"),
        "ed_diagnosis": os.path.join(ed, "diagnosis.csv.gz"),
        "discharge": os.path.join(note, "discharge.csv.gz"),
        "radiology": os.path.join(note, "radiology.csv.gz"),
    }


def csv(path: str) -> str:
    """生成一个 DuckDB read_csv 表达式: 全列按 VARCHAR 读, 避免 MIMIC 脏数据触发类型推断错误。"""
    return (
        f"read_csv('{path}', all_varchar=true, header=true, "
        f"compression='gzip', sample_size=-1, ignore_errors=true)"
    )


# --------------------------------------------------------------------------- #
# Stage 1: cohort —— 队列筛选 + 一病人一例 + 出院小结文本
# --------------------------------------------------------------------------- #
def build_cohort(con: duckdb.DuckDBPyConnection, p: Dict[str, str], args) -> int:
    """筛选队列并物化为 DuckDB 表 cohort, 返回行数。

    纳入: 成人、经 ED 入院、有主诊断(seq_num=1)、有出院小结(长度足够);
    排除: 外伤/产科/Z码等"无需诊断推理"的主诊断;
    去相关: 每个病人只保留出院小结最长的一次住院(subject-disjoint)。
    """
    limit_clause = f"LIMIT {args.limit}" if args.limit and args.limit > 0 else ""
    # 主诊断 ICD 版本过滤: 默认只取 ICD-10(不使用 ICD-9), 也可设 'any'/'9'
    if str(args.icd_version) == "any":
        icd_version_clause = ""
    else:
        icd_version_clause = f"AND pd.icd_version = {int(args.icd_version)}"

    # 排除院内死亡(默认): 死亡病例诊断 ground truth 有效, 但与"可对话"高度冲突。
    expired_clause = "AND a.hospital_expire_flag = 0" if args.exclude_expired else ""

    # 排除"无法对话"的主诊断类别(昏迷/心脏骤停/缺氧脑病等)。ICD-10 码不含小数点。
    prefixes = [s.strip().replace(".", "") for s in args.exclude_icd_prefixes.split(",") if s.strip()]
    if prefixes:
        icd_block_clause = (
            "AND NOT (pd.icd_version = 10 AND regexp_matches(pd.icd_code, '^("
            + "|".join(prefixes)
            + ")'))"
        )
    else:
        icd_block_clause = ""
    sql = f"""
    CREATE OR REPLACE TABLE cohort AS
    WITH pat AS (
        SELECT CAST(subject_id AS BIGINT) AS subject_id,
               gender,
               CAST(anchor_age AS INTEGER) AS anchor_age,
               CAST(anchor_year AS INTEGER) AS anchor_year,
               anchor_year_group
        FROM {csv(p['patients'])}
    ),
    adm AS (
        SELECT CAST(subject_id AS BIGINT) AS subject_id,
               CAST(hadm_id AS BIGINT)   AS hadm_id,
               CAST(admittime AS TIMESTAMP) AS admittime,
               CAST(dischtime AS TIMESTAMP) AS dischtime,
               admission_type, race, marital_status, language,
               TRY_CAST(edregtime AS TIMESTAMP) AS edregtime,
               CAST(hospital_expire_flag AS INTEGER) AS hospital_expire_flag
        FROM {csv(p['admissions'])}
    ),
    eds AS (
        SELECT CAST(subject_id AS BIGINT) AS subject_id,
               TRY_CAST(hadm_id AS BIGINT) AS hadm_id,
               CAST(stay_id AS BIGINT) AS stay_id,
               TRY_CAST(intime AS TIMESTAMP) AS ed_intime,
               arrival_transport, disposition
        FROM {csv(p['edstays'])}
        WHERE hadm_id IS NOT NULL
    ),
    dx AS (
        SELECT CAST(subject_id AS BIGINT) AS subject_id,
               CAST(hadm_id AS BIGINT) AS hadm_id,
               CAST(seq_num AS INTEGER) AS seq_num,
               icd_code, CAST(icd_version AS INTEGER) AS icd_version
        FROM {csv(p['diagnoses_icd'])}
    ),
    ddx AS (
        SELECT icd_code, CAST(icd_version AS INTEGER) AS icd_version, long_title
        FROM {csv(p['d_icd_diagnoses'])}
    ),
    disch AS (
        SELECT note_id,
               CAST(subject_id AS BIGINT) AS subject_id,
               CAST(hadm_id AS BIGINT) AS hadm_id,
               TRY_CAST(charttime AS TIMESTAMP) AS note_charttime,
               text AS note_text,
               length(text) AS note_len
        FROM {csv(p['discharge'])}
    ),
    primary_dx AS (
        SELECT d.subject_id, d.hadm_id, d.icd_code, d.icd_version, t.long_title
        FROM dx d
        JOIN ddx t ON d.icd_code = t.icd_code AND d.icd_version = t.icd_version
        WHERE d.seq_num = 1
    ),
    base AS (
        SELECT a.subject_id, a.hadm_id, e.stay_id,
               (p2.anchor_age + (EXTRACT(year FROM a.admittime) - p2.anchor_year)) AS age,
               p2.gender, a.race, a.marital_status, a.language, a.admission_type,
               a.admittime, a.dischtime, a.edregtime, e.ed_intime,
               e.arrival_transport, a.hospital_expire_flag,
               pd.icd_code, pd.icd_version, pd.long_title,
               ds.note_id, ds.note_charttime, ds.note_text, ds.note_len
        FROM adm a
        JOIN pat p2          USING (subject_id)
        JOIN eds e           ON e.subject_id = a.subject_id AND e.hadm_id = a.hadm_id
        JOIN primary_dx pd   ON pd.subject_id = a.subject_id AND pd.hadm_id = a.hadm_id
        JOIN disch ds        ON ds.subject_id = a.subject_id AND ds.hadm_id = a.hadm_id
        WHERE (p2.anchor_age + (EXTRACT(year FROM a.admittime) - p2.anchor_year)) >= 18
          AND ds.note_len > {args.min_note_len}
          AND EXTRACT(EPOCH FROM (a.dischtime - a.admittime)) / 3600.0 >= {args.min_los_hours}
          {icd_version_clause}
          {expired_clause}
          {icd_block_clause}
          -- 排除"无诊断推理"的主诊断
          AND NOT (pd.icd_version = 10 AND regexp_matches(pd.icd_code, '^[STOZ]'))
          AND NOT (pd.icd_version = 9  AND (
                    regexp_matches(pd.icd_code, '^[EV]')
                 OR TRY_CAST(substr(pd.icd_code, 1, 3) AS INTEGER) BETWEEN 800 AND 999))
    ),
    ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY subject_id
                                  ORDER BY note_len DESC, hadm_id) AS rn
        FROM base
    )
    SELECT * EXCLUDE (rn)
    FROM ranked
    WHERE rn = 1
    ORDER BY subject_id
    {limit_clause}
    """
    con.execute(sql)
    n = con.execute("SELECT COUNT(*) FROM cohort").fetchone()[0]
    return n


# --------------------------------------------------------------------------- #
# Stage 2: 分诊主诉 + 生命体征(ED triage), 小表直接 join cohort
# --------------------------------------------------------------------------- #
def build_triage(con: duckdb.DuckDBPyConnection, p: Dict[str, str]) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE triage AS
        SELECT c.hadm_id,
               t.chiefcomplaint,
               TRY_CAST(t.temperature AS DOUBLE) AS temperature,
               TRY_CAST(t.heartrate   AS DOUBLE) AS heartrate,
               TRY_CAST(t.resprate    AS DOUBLE) AS resprate,
               TRY_CAST(t.o2sat       AS DOUBLE) AS o2sat,
               TRY_CAST(t.sbp         AS DOUBLE) AS sbp,
               TRY_CAST(t.dbp         AS DOUBLE) AS dbp,
               t.pain,
               TRY_CAST(t.acuity      AS INTEGER) AS acuity
        FROM {csv(p['triage'])} t
        JOIN cohort c ON CAST(t.stay_id AS BIGINT) = c.stay_id
        """
    )


def build_ed_diagnosis(con: duckdb.DuckDBPyConnection, p: Dict[str, str]) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE ed_dx AS
        SELECT c.hadm_id,
               CAST(d.seq_num AS INTEGER) AS seq_num,
               d.icd_code, CAST(d.icd_version AS INTEGER) AS icd_version, d.icd_title
        FROM {csv(p['ed_diagnosis'])} d
        JOIN cohort c ON CAST(d.stay_id AS BIGINT) = c.stay_id
        """
    )


# --------------------------------------------------------------------------- #
# Stage 3: 化验(labevents, 大表) —— 半连接 + 诊断期时间窗 + 出院检查除外
# --------------------------------------------------------------------------- #
def build_labs(con: duckdb.DuckDBPyConnection, p: Dict[str, str], args) -> None:
    # 诊断期窗口起点: ED 登记时间优先, 否则入院时间。
    # 窗口终点: 起点 + lab_window_hours, 且不晚于出院前 discharge_buffer_hours(出院检查除外)。
    if args.lab_window_hours and args.lab_window_hours > 0:
        upper = (
            f"LEAST(start_ts + INTERVAL '{args.lab_window_hours} hours', "
            f"c.dischtime - INTERVAL '{args.discharge_buffer_hours} hours')"
        )
    else:
        upper = f"c.dischtime - INTERVAL '{args.discharge_buffer_hours} hours'"

    dedup = (
        "QUALIFY ROW_NUMBER() OVER "
        "(PARTITION BY r.hadm_id, r.itemid ORDER BY r.charttime) = 1"
        if args.first_per_item
        else ""
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE labs AS
        WITH win AS (
            SELECT c.hadm_id,
                   COALESCE(c.edregtime, c.ed_intime, c.admittime) AS start_ts,
                   {upper} AS end_ts
            FROM cohort c
        ),
        raw AS (
            SELECT CAST(l.hadm_id AS BIGINT) AS hadm_id,
                   CAST(l.itemid  AS BIGINT) AS itemid,
                   TRY_CAST(l.charttime AS TIMESTAMP) AS charttime,
                   l.value,
                   TRY_CAST(l.valuenum AS DOUBLE) AS valuenum,
                   l.valueuom,
                   TRY_CAST(l.ref_range_lower AS DOUBLE) AS ref_range_lower,
                   TRY_CAST(l.ref_range_upper AS DOUBLE) AS ref_range_upper,
                   l.flag
            FROM {csv(p['labevents'])} l
            SEMI JOIN cohort c ON CAST(l.hadm_id AS BIGINT) = c.hadm_id
            WHERE l.hadm_id IS NOT NULL AND l.charttime IS NOT NULL
        )
        SELECT r.hadm_id, r.itemid, di.label, di.fluid, di.category,
               r.charttime, r.value, r.valuenum, r.valueuom,
               r.ref_range_lower, r.ref_range_upper, r.flag
        FROM raw r
        JOIN win w ON r.hadm_id = w.hadm_id
        LEFT JOIN {csv(p['d_labitems'])} di ON r.itemid = CAST(di.itemid AS BIGINT)
        WHERE r.charttime >= w.start_ts AND r.charttime <= w.end_ts
          AND (r.value IS NOT NULL OR r.valuenum IS NOT NULL)
          -- A3: 剔除溶血/黄疸/脂血质量指数(H/I/L), 非临床结果
          AND r.itemid NOT IN (50934, 50947, 51678)
        {dedup}
        """
    )


# --------------------------------------------------------------------------- #
# Stage 4: 影像报告(radiology note, 大表)
# --------------------------------------------------------------------------- #
def build_radiology(con: duckdb.DuckDBPyConnection, p: Dict[str, str], args) -> None:
    if args.lab_window_hours and args.lab_window_hours > 0:
        upper = (
            f"LEAST(start_ts + INTERVAL '{args.lab_window_hours} hours', "
            f"c.dischtime - INTERVAL '{args.discharge_buffer_hours} hours')"
        )
    else:
        upper = f"c.dischtime - INTERVAL '{args.discharge_buffer_hours} hours'"
    con.execute(
        f"""
        CREATE OR REPLACE TABLE radiology AS
        WITH win AS (
            SELECT c.hadm_id,
                   COALESCE(c.edregtime, c.ed_intime, c.admittime) AS start_ts,
                   {upper} AS end_ts
            FROM cohort c
        )
        SELECT r2.hadm_id, r2.note_type, r2.charttime, r2.text
        FROM (
            SELECT CAST(r.hadm_id AS BIGINT) AS hadm_id,
                   r.note_type,
                   TRY_CAST(r.charttime AS TIMESTAMP) AS charttime,
                   r.text
            FROM {csv(p['radiology'])} r
            SEMI JOIN cohort c ON CAST(r.hadm_id AS BIGINT) = c.hadm_id
            WHERE r.hadm_id IS NOT NULL
        ) r2
        JOIN win w ON r2.hadm_id = w.hadm_id
        WHERE r2.charttime IS NULL OR (r2.charttime >= w.start_ts AND r2.charttime <= w.end_ts)
        """
    )


# --------------------------------------------------------------------------- #
# Stage 5: 微生物(microbiologyevents, 中等表)
# --------------------------------------------------------------------------- #
def build_micro(con: duckdb.DuckDBPyConnection, p: Dict[str, str], args) -> None:
    if args.lab_window_hours and args.lab_window_hours > 0:
        upper = (
            f"LEAST(start_ts + INTERVAL '{args.lab_window_hours} hours', "
            f"c.dischtime - INTERVAL '{args.discharge_buffer_hours} hours')"
        )
    else:
        upper = f"c.dischtime - INTERVAL '{args.discharge_buffer_hours} hours'"
    con.execute(
        f"""
        CREATE OR REPLACE TABLE micro AS
        WITH win AS (
            SELECT c.hadm_id,
                   COALESCE(c.edregtime, c.ed_intime, c.admittime) AS start_ts,
                   {upper} AS end_ts
            FROM cohort c
        )
        SELECT m2.hadm_id, m2.charttime, m2.spec_type_desc, m2.test_name,
               m2.org_name, m2.ab_name, m2.interpretation
        FROM (
            SELECT CAST(m.hadm_id AS BIGINT) AS hadm_id,
                   COALESCE(TRY_CAST(m.charttime AS TIMESTAMP),
                            TRY_CAST(m.chartdate AS TIMESTAMP)) AS charttime,
                   m.spec_type_desc, m.test_name, m.org_name, m.ab_name, m.interpretation
            FROM {csv(p['microbiologyevents'])} m
            SEMI JOIN cohort c ON CAST(m.hadm_id AS BIGINT) = c.hadm_id
            WHERE m.hadm_id IS NOT NULL
        ) m2
        JOIN win w ON m2.hadm_id = w.hadm_id
        WHERE m2.charttime IS NULL OR (m2.charttime >= w.start_ts AND m2.charttime <= w.end_ts)
        """
    )


# --------------------------------------------------------------------------- #
# 工具: 把查询结果按 hadm_id 分组为 dict
# --------------------------------------------------------------------------- #
def fetch_grouped(con: duckdb.DuckDBPyConnection, table: str) -> Dict[int, List[dict]]:
    try:
        cur = con.execute(f"SELECT * FROM {table}")
    except duckdb.Error:
        return {}
    cols = [d[0] for d in cur.description]
    grouped: Dict[int, List[dict]] = {}
    for row in cur.fetchall():
        rec = dict(zip(cols, row))
        hadm = rec.pop("hadm_id")
        grouped.setdefault(hadm, []).append(rec)
    return grouped


def jsonify(value):
    """把 datetime 等不可序列化对象转成字符串。"""
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ")
    return value


# --------------------------------------------------------------------------- #
# A2/A3: 化验清洗(de-id 回填 + 垃圾行过滤)
# --------------------------------------------------------------------------- #
# 非临床结果的 value 文本(多为血气标本描述/注释泄漏), 在 valuenum 缺失时视为垃圾。
_LAB_VALUE_JUNK_RE = re.compile(
    r"(?i)^\s*(hold|ven\.?$|art\.?$|mix\.?$|intubated|not intubated|controlled|"
    r"done|none$|see |discard|specimen|received|cancel|error|unable|random|"
    r"multiple|pending|hemolyzed|sent|added|spontaneous|needle|nasal|cannula|"
    r"mask|room air|ventilat|cpap|bipap|self|i/e$|line$|catheter)"
)


def _fmt_num(x) -> Optional[str]:
    try:
        f = float(x)
        return str(int(f)) if f.is_integer() else str(f)
    except (TypeError, ValueError):
        return str(x) if x is not None else None


def lab_value_display(value, valuenum) -> Optional[str]:
    """A2: 当结构化 value 被去标识(`___`)或缺失时, 用 valuenum 回填可读值。"""
    masked = value is None or str(value).strip().strip("_") == ""
    if masked:
        return _fmt_num(valuenum) if valuenum is not None else None
    return str(value).strip()


def is_junk_lab(lab: dict) -> bool:
    """A3: 判断一条化验是否为垃圾行(无 valuenum 且 value 为标本描述/注释/占位符)。"""
    if lab.get("valuenum") is not None:
        return False  # 有数值一律保留
    v = lab.get("value")
    if v is None or str(v).strip().strip("_") == "":
        return True
    return bool(_LAB_VALUE_JUNK_RE.match(str(v)))


# --------------------------------------------------------------------------- #
# Stage 6: 组装 case JSON
# --------------------------------------------------------------------------- #
def assemble(con: duckdb.DuckDBPyConnection, out_dir: Path, args) -> dict:
    cases_dir = out_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    labs_by_hadm = fetch_grouped(con, "labs")
    rad_by_hadm = fetch_grouped(con, "radiology")
    micro_by_hadm = fetch_grouped(con, "micro")
    triage_by_hadm = fetch_grouped(con, "triage")
    ed_dx_by_hadm = fetch_grouped(con, "ed_dx")

    cur = con.execute("SELECT * FROM cohort ORDER BY subject_id")
    cols = [d[0] for d in cur.description]

    jsonl_path = out_dir / "dataset.jsonl"
    n_written = 0
    n_skipped_no_hpi = 0
    n_skipped_nonconversable = 0
    src_counter: Dict[str, int] = {}
    stats = {"n_cases": 0, "with_labs": 0, "with_radiology": 0, "with_micro": 0}

    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for row in cur.fetchall():
            r = dict(zip(cols, row))
            hadm = r["hadm_id"]

            sections = segment_note(r["note_text"])
            planes = split_to_planes(sections)

            # 质量门槛: 没有 HPI 或 HPI 过短 → 跳过(无足够病史无法 simulate)
            hpi = planes["history"].get("hpi", "")
            if len(hpi) < args.min_hpi_len:
                n_skipped_no_hpi += 1
                continue

            triage = triage_by_hadm.get(hadm, [{}])
            triage = triage[0] if triage else {}

            case_id = f"inp_{r['subject_id']}_{hadm}"
            cc = planes["history"].get("chief_complaint") or triage.get("chiefcomplaint")

            # 可对话性闸门(Stage A): 重症昏迷/无意识/插管镇静 → 患者无法应答问诊
            pe_text = planes["exam"].get("physical_exam")
            cc_for_check = " ".join(
                x for x in [cc, triage.get("chiefcomplaint")] if x
            )
            conversable, conv_reasons = is_conversable(cc_for_check, hpi, pe_text)
            history_source = detect_history_source(hpi)
            if not conversable and args.require_conversable:
                n_skipped_nonconversable += 1
                continue
            src_counter[history_source] = src_counter.get(history_source, 0) + 1

            # A2/A3: 化验清洗(回填 valuenum + 剔除垃圾行) → A5: 计算 lab_anchor_time
            raw_labs = sorted(
                labs_by_hadm.get(hadm, []),
                key=lambda x: (x.get("charttime") is None, x.get("charttime")),
            )
            labs = []
            for lab in raw_labs:
                if is_junk_lab(lab):
                    continue
                d = {k: jsonify(v) for k, v in lab.items()}
                d["value_display"] = lab_value_display(lab.get("value"), lab.get("valuenum"))
                labs.append(d)
            rads = rad_by_hadm.get(hadm, [])
            micros = micro_by_hadm.get(hadm, [])

            # A5: 诊断期窗口与化验锚点(显式落 provenance, 保证可复现)
            start_ts = r.get("edregtime") or r.get("ed_intime") or r.get("admittime")
            end_ts = None
            if start_ts is not None:
                win_end = start_ts + _dt.timedelta(hours=args.lab_window_hours) if args.lab_window_hours else None
                disch_cap = (
                    r["dischtime"] - _dt.timedelta(hours=args.discharge_buffer_hours)
                    if r.get("dischtime") is not None
                    else None
                )
                cands = [t for t in (win_end, disch_cap) if t is not None]
                end_ts = min(cands) if cands else None
            lab_anchor_time = labs[0]["charttime"] if labs else None

            case = {
                "case_id": case_id,
                "source": {
                    "subject_id": r["subject_id"],
                    "hadm_id": hadm,
                    "stay_id": r["stay_id"],
                    "note_id": r["note_id"],
                    "dataset": "MIMIC-IV (hosp+ed+note)",
                },
                "demographics": {
                    "age": r["age"],
                    "gender": r["gender"],
                    "race": r["race"],
                    "marital_status": r["marital_status"],
                    "language": r["language"],
                },
                # Doctor agent 初始唯一可见(分诊台呈现)
                "presentation": {
                    "chief_complaint": cc,
                    "admission_type": r["admission_type"],
                    "arrival_transport": r["arrival_transport"],
                    "acuity": triage.get("acuity"),
                    "triage_vitals": {
                        "temperature": triage.get("temperature"),
                        "heartrate": triage.get("heartrate"),
                        "resprate": triage.get("resprate"),
                        "o2sat": triage.get("o2sat"),
                        "sbp": triage.get("sbp"),
                        "dbp": triage.get("dbp"),
                        "pain": triage.get("pain"),
                    },
                },
                # G_hist: 患者入院时可知 —— Patient agent 的剧本
                "history": planes["history"],
                # G_exam: 检查可得(出院检查除外) —— Exam agent 的环境
                "exams": {
                    "physical_exam_admission": planes["exam"].get("physical_exam"),
                    "pertinent_results_text": planes["exam"].get("pertinent_results"),
                    "labs": [
                        {k: jsonify(v) for k, v in lab.items()} for lab in labs
                    ],
                    "radiology": [
                        {k: jsonify(v) for k, v in rad.items()} for rad in rads
                    ],
                    "microbiology": [
                        {k: jsonify(v) for k, v in mic.items()} for mic in micros
                    ],
                },
                # G_dx: 隐藏答案 —— 对两个 agent 都不可见, 仅 Evaluator 使用
                "diagnosis_hidden": {
                    "primary_icd": r["icd_code"],
                    "icd_version": r["icd_version"],
                    "title": r["long_title"],
                    "discharge_diagnosis_text": planes["diagnosis"].get(
                        "discharge_diagnosis"
                    ),
                    "ed_diagnosis": [
                        {
                            "seq_num": d["seq_num"],
                            "icd_code": d["icd_code"],
                            "icd_title": d["icd_title"],
                        }
                        for d in sorted(
                            ed_dx_by_hadm.get(hadm, []),
                            key=lambda x: x.get("seq_num") or 0,
                        )
                    ],
                },
                "provenance": {
                    "admittime": jsonify(r["admittime"]),
                    "dischtime": jsonify(r["dischtime"]),
                    "los_hours": round(
                        (r["dischtime"] - r["admittime"]).total_seconds() / 3600.0, 1
                    )
                    if r["admittime"] and r["dischtime"]
                    else None,
                    "icd_version_filter": args.icd_version,
                    "lab_window_hours": args.lab_window_hours,
                    "discharge_buffer_hours": args.discharge_buffer_hours,
                    "lab_window_start": jsonify(start_ts),
                    "lab_window_end": jsonify(end_ts),
                    "lab_anchor_time": lab_anchor_time,
                    "history_source": history_source,
                    "conversable": conversable,
                    "conversable_reasons": conv_reasons,
                    "note_len": r["note_len"],
                    "note_deid_density": deid_density(r["note_text"]),
                    "sections_found": sorted(sections.keys()),
                    "n_labs": len(labs),
                    "n_radiology": len(rads),
                    "n_micro": len(micros),
                },
            }

            jf.write(json.dumps(case, ensure_ascii=False) + "\n")
            with open(cases_dir / f"{case_id}.json", "w", encoding="utf-8") as cf:
                json.dump(case, cf, ensure_ascii=False, indent=2)

            n_written += 1
            stats["n_cases"] += 1
            stats["with_labs"] += 1 if labs else 0
            stats["with_radiology"] += 1 if rads else 0
            stats["with_micro"] += 1 if micros else 0

    stats["skipped_no_hpi"] = n_skipped_no_hpi
    stats["skipped_nonconversable"] = n_skipped_nonconversable
    stats["history_source"] = src_counter
    stats["jsonl"] = str(jsonl_path)
    with open(out_dir / "stats.json", "w", encoding="utf-8") as sf:
        json.dump(stats, sf, ensure_ascii=False, indent=2)
    return stats


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help="MIMIC-IV 三模块所在根目录")
    ap.add_argument("--out", default=str(Path(__file__).parent / "output"), help="输出目录")
    ap.add_argument("--limit", type=int, default=200, help="cohort 上限(0=全量)")
    ap.add_argument(
        "--icd-version",
        choices=["10", "9", "any"],
        default="10",
        help="主诊断 ICD 版本: 默认 10(只用 ICD-10, 不使用 ICD-9); any=两者都要",
    )
    ap.add_argument("--min-note-len", type=int, default=1500, help="出院小结最短字符数")
    ap.add_argument("--min-hpi-len", type=int, default=200, help="HPI 段最短字符数")
    ap.add_argument("--min-los-hours", type=float, default=24.0, help="最短住院时长(小时)")
    ap.add_argument(
        "--keep-expired",
        dest="exclude_expired",
        action="store_false",
        help="保留院内死亡病例(默认排除)",
    )
    ap.set_defaults(exclude_expired=True)
    ap.add_argument(
        "--exclude-icd-prefixes",
        default="R40.2,I46,G93.1",
        help="排除的主诊断 ICD-10 前缀(逗号分隔): 昏迷/心脏骤停/缺氧脑病等无法对话类别",
    )
    ap.add_argument(
        "--keep-nonconversable",
        dest="require_conversable",
        action="store_false",
        help="保留无法对话病例(默认排除: 重症昏迷/无意识/插管镇静)",
    )
    ap.set_defaults(require_conversable=True)
    ap.add_argument(
        "--lab-window-hours",
        type=float,
        default=72.0,
        help="诊断期化验/影像/微生物的时间窗(从 ED 登记/入院起算; 0=取到出院前)",
    )
    ap.add_argument(
        "--discharge-buffer-hours",
        type=float,
        default=0.0,
        help=(
            "出院检查除外的额外缓冲: 丢弃出院前该小时数内的检查。"
            "默认 0 —— 主要靠 lab-window-hours(从入院起算的诊断期窗口)排除出院检查; "
            "对短住院设过大缓冲会误删全部检查。"
        ),
    )
    ap.add_argument(
        "--all-lab-values",
        dest="first_per_item",
        action="store_false",
        help="保留窗口内每项化验的全部时间点(默认只取每项最早一次)",
    )
    ap.set_defaults(first_per_item=True)
    ap.add_argument(
        "--stages",
        default="all",
        help="逗号分隔: cohort,triage,labs,radiology,micro,assemble 或 all",
    )
    ap.add_argument("--threads", type=int, default=4, help="DuckDB 线程数")
    ap.add_argument("--memory-limit", default="6GB", help="DuckDB 内存上限")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    paths = module_paths(args.data_root)

    missing = [name for name, fp in paths.items() if not os.path.exists(fp)]
    if missing:
        print(f"[ERROR] 缺少数据文件: {missing}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    stages = (
        {"cohort", "triage", "labs", "radiology", "micro", "assemble"}
        if args.stages == "all"
        else {s.strip() for s in args.stages.split(",")}
    )

    con = duckdb.connect(database=str(out_dir / "mimic_work.duckdb"))
    con.execute(f"PRAGMA threads={args.threads}")
    con.execute(f"PRAGMA memory_limit='{args.memory_limit}'")
    con.execute("PRAGMA enable_progress_bar")

    def step(name, fn):
        t0 = time.time()
        print(f"[stage] {name} ...", flush=True)
        fn()
        print(f"[stage] {name} done in {time.time() - t0:.1f}s", flush=True)

    if "cohort" in stages:
        t0 = time.time()
        print("[stage] cohort ...", flush=True)
        n = build_cohort(con, paths, args)
        print(f"[stage] cohort done: {n} cases in {time.time() - t0:.1f}s", flush=True)

    # cohort 是后续所有 stage 的依赖
    n_cohort = con.execute(
        "SELECT COUNT(*) FROM cohort"
    ).fetchone()[0] if _table_exists(con, "cohort") else 0
    if n_cohort == 0:
        print("[ERROR] cohort 为空, 后续 stage 无法执行。", file=sys.stderr)
        return 1

    if "triage" in stages:
        step("triage", lambda: (build_triage(con, paths), build_ed_diagnosis(con, paths)))
    if "labs" in stages:
        step("labs", lambda: build_labs(con, paths, args))
    if "radiology" in stages:
        step("radiology", lambda: build_radiology(con, paths, args))
    if "micro" in stages:
        step("micro", lambda: build_micro(con, paths, args))
    if "assemble" in stages:
        t0 = time.time()
        print("[stage] assemble ...", flush=True)
        stats = assemble(con, out_dir, args)
        print(
            f"[stage] assemble done in {time.time() - t0:.1f}s -> {stats}", flush=True
        )

    con.close()
    print(f"[done] 输出目录: {out_dir}", flush=True)
    return 0


def _table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return (
        con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [name],
        ).fetchone()[0]
        > 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
