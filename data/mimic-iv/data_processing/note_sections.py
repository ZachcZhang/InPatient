"""出院小结(discharge summary)分段器。

MIMIC-IV-Note 的出院小结段落标题较为规整, 这里用"锚定行首、大小写不敏感"
的正则做一级确定性切分, 把自由文本切成命名段落。切分结果再按时间平面归位:

  - G_hist  (患者入院时已知): Chief Complaint / HPI / ROS / PMH / 手术史 /
            Social History / Family History / Allergies / Medications on Admission
  - G_exam  (客观、检查可得): Physical Exam(**仅入院 admission, 出院检查除外**) /
            Pertinent Results
  - G_dx    (隐藏答案):      Discharge Diagnosis

注意: Brief Hospital Course / Discharge* 等段落叙述的是"住院后"过程, 属于会
泄漏答案的内容, 默认不进入任何对 agent 可见的平面。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# 规范段名 -> 该段可能出现的标题写法(全部按 行首 + 大小写不敏感 匹配)。
# 顺序无关, 解析时会按它们在文本中出现的真实位置切片。
SECTION_HEADERS: Dict[str, List[str]] = {
    # ---- G_hist: 患者可知 ----
    "chief_complaint": ["Chief Complaint"],
    "hpi": ["History of Present Illness", "HPI"],
    "ros": ["Review of Systems", "ROS"],
    "pmh": ["Past Medical History", "PMH", "Medical History"],
    "psh": ["Past Surgical History", "Surgical History"],
    "social_history": ["Social History", "SH"],
    "family_history": ["Family History", "FH"],
    "allergies": ["Allergies"],
    "medications_on_admission": [
        "Medications on Admission",
        "Home Medications",
        "Admission Medications",
    ],
    # ---- G_exam: 检查可得 ----
    "physical_exam": [
        "Physical Exam",
        "Physical Examination",
        "PHYSICAL EXAMINATION",
        "Admission Exam",
    ],
    "pertinent_results": ["Pertinent Results", "Laboratory Data", "Labs"],
    # ---- G_dx: 隐藏答案 ----
    "discharge_diagnosis": [
        "Discharge Diagnosis",
        "Discharge Diagnoses",
        "Final Diagnosis",
        "Final Diagnoses",
    ],
    # ---- 仅用于构造 / 需排除的住院后叙事 + 泄漏答案的字段 ----
    # 注: "Major Surgical or Invasive Procedure" 紧跟在 Chief Complaint 之后,
    # 必须显式识别, 否则会被并入 CC 并直接泄漏术式答案。
    "major_procedure": [
        "Major Surgical or Invasive Procedure",
        "Major Surgical or Invasive Procedures",
    ],
    "brief_hospital_course": [
        "Brief Hospital Course",
        "Hospital Course",
        "Concise Summary of Hospital Course",
    ],
    "discharge_condition": ["Discharge Condition"],
    "discharge_medications": ["Discharge Medications"],
    "discharge_instructions": ["Discharge Instructions"],
    "discharge_disposition": ["Discharge Disposition"],
    "followup": ["Followup Instructions", "Follow-up Instructions"],
    # ---- 行政/抬头字段 ----
    # 仅用于把相邻段落"切干净"(例如 Attending 紧跟在 Allergies 之后,
    # 不识别会把 "Attending: ___" 并进 allergies)。全部归入 hidden。
    "_admin": [
        "Attending",
        "Service",
        "Facility",
    ],
}

# 平面归属
PLANE_OF_SECTION: Dict[str, str] = {
    "chief_complaint": "history",
    "hpi": "history",
    "ros": "history",
    "pmh": "history",
    "psh": "history",
    "social_history": "history",
    "family_history": "history",
    "allergies": "history",
    "medications_on_admission": "history",
    "physical_exam": "exam",
    "pertinent_results": "exam",
    "discharge_diagnosis": "diagnosis",
    # 其余默认 hidden(住院后叙事), 不分配给任何 agent
}

# 标题文本(小写) -> 规范段名, 用于把匹配到的标题归一化
_HEADER_LOOKUP: Dict[str, str] = {}
for _canon, _variants in SECTION_HEADERS.items():
    for _v in _variants:
        _HEADER_LOOKUP[_v.lower()] = _canon

# 主正则: 行首(允许前导空白) + 任一已知标题 + 冒号。
# 用 sorted(.., len desc) 保证 "History of Present Illness" 先于 "HPI" 之类被匹配到。
_ALL_VARIANTS = sorted(
    {v for vs in SECTION_HEADERS.values() for v in vs}, key=len, reverse=True
)
_HEADER_RE = re.compile(
    r"^[ \t]*(" + "|".join(re.escape(v) for v in _ALL_VARIANTS) + r")[ \t]*:",
    re.IGNORECASE | re.MULTILINE,
)

# 物理检查中"出院检查"的起始标记 —— 命中后截断, 只保留入院部分。
# 不强制行首锚定: MIMIC 中该标记常以 `--DISCHARGE EXAM--`、`VSS on discharge`、
# `physical examination upon discharge:` 等内联形式出现。这些短语在查体段里
# 几乎只表示"出院时", 极少指分泌物(分泌物多为 "no/purulent/nasal discharge"),
# 因此可安全地在最早命中处截断(宁可保守多截, 也不漏 leakage)。
_DISCHARGE_EXAM_RE = re.compile(
    r"(?i)(?:"
    r"discharge\s+(?:physical\s+)?exam(?:ination)?"
    r"|(?:physical\s+)?exam(?:ination)?\s+(?:up)?on\s+discharge"
    r"|exam\s+at\s+discharge"
    r"|(?:up)?on\s+discharge"
    r"|d\/?c\s+exam"
    r"|discharge\s+pe\b"
    r")"
)

# 入院检查的标签行(去掉这些纯标签行, 保留正文)。
_ADMISSION_EXAM_LABEL_RE = re.compile(
    r"(?im)^\s*(?:"
    r"admission\s+(?:physical\s+)?exam(?:ination)?"
    r"|(?:physical\s+)?exam(?:ination)?\s+(?:up)?on\s+admission"
    r"|exam\s+(?:up)?on\s+admission"
    r"|(?:up)?on\s+admission"
    r"|admission\s+pe"
    r")\b\s*:?\s*$"
)


def segment_note(text: str) -> Dict[str, str]:
    """把出院小结切成 {规范段名: 段落正文}。未匹配到的段落不会出现在结果里。"""
    if not text:
        return {}

    matches = list(_HEADER_RE.finditer(text))
    if not matches:
        return {}

    sections: Dict[str, str] = {}
    for i, m in enumerate(matches):
        canon = _HEADER_LOOKUP.get(m.group(1).strip().lower())
        if canon is None:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        # 同名段落多次出现时, 保留信息最丰富的一段
        if canon not in sections or len(body) > len(sections[canon]):
            sections[canon] = body
    return sections


def admission_physical_exam(physical_exam_text: Optional[str]) -> Optional[str]:
    """从 Physical Exam 段中只取"入院查体", 丢弃"出院查体"(出院检查除外)。

    策略:
      1. 若存在"出院检查"标记, 截断其之前的内容;
      2. 去掉残留的"入院检查"标签行;
      3. 返回清洗后的入院查体文本。
    """
    if not physical_exam_text:
        return None

    txt = physical_exam_text
    m = _DISCHARGE_EXAM_RE.search(txt)
    if m is not None:
        txt = txt[: m.start()]

    txt = _ADMISSION_EXAM_LABEL_RE.sub("", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return txt or None


def deid_density(text: Optional[str]) -> float:
    """去标识占位符 `___` 的密度(占位符字符数 / 文本长度), 用于质量过滤。"""
    if not text:
        return 0.0
    placeholder_chars = sum(len(s) for s in re.findall(r"_{2,}", text))
    return round(placeholder_chars / max(len(text), 1), 4)


def split_to_planes(sections: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """把切分好的段落按 history / exam / diagnosis / hidden 平面归位。

    其中 Physical Exam 自动只保留入院查体(出院检查除外)。
    """
    planes: Dict[str, Dict[str, str]] = {
        "history": {},
        "exam": {},
        "diagnosis": {},
        "hidden": {},
    }
    for canon, body in sections.items():
        plane = PLANE_OF_SECTION.get(canon, "hidden")
        if canon == "physical_exam":
            body = admission_physical_exam(body) or ""
            if not body:
                continue
        planes[plane][canon] = body
    return planes


# =========================================================================== #
# 可对话性 / 病史来源判定(Stage A 确定性闸门)
#
# 目的: patient simulator 的前提是"患者本人能应答问诊"。下面用确定性规则拦截
# 重症昏迷/无意识/插管镇静等无法对话的病例, 并标注病史来源(本人 / 旁人代述)。
# 注意: 晕厥(syncope)患者会清醒并能复述发作, **不应**被误杀。
# 句子级的精细角色判定交给 Stage B 的本地 LLM(B1 Source Attribution Scorer)。
# =========================================================================== #

# 主诉黑名单: 命中即视为"就诊时无法对话"。
_CC_BLOCK_RE = re.compile(
    r"(?i)\b("
    r"altered mental status|\bAMS\b|unresponsive|unconscious|comatose|\bcoma\b"
    r"|cardiac arrest|s/?p arrest|cardiopulmonary arrest|\bcpr\b|code blue"
    r"|found down|not responding|obtunded|unarousable"
    r")\b"
)

# HPI 中"硬性"无法对话信号(命中即排除)。
_HPI_HARD_NONCONVERSABLE_RE = re.compile(
    r"(?i)("
    r"unable to (provide|give|obtain|offer) (a )?history"
    r"|intubated and sedated|intubated, sedated|sedated and intubated"
    r"|currently (intubated|sedated)|remains (intubated|sedated)"
    r"|nonverbal|non-verbal|minimally responsive|unresponsive|obtunded|comatose"
    r"|gcs (of )?[3-7]\b|gcs [3-7]/15"
    r"|unable to be interviewed|cannot provide (any )?history"
    r")"
)

# HPI 中"软性"旁人/病历代述信号(打标记, 不一定排除)。
_HPI_COLLATERAL_RE = re.compile(
    r"(?i)("
    r"per (his|her|the) (wife|husband|family|daughter|son|mother|father|partner|caregiver|spouse|sister|brother)"
    r"|history (is )?obtained from (the )?(chart|family|record|wife|husband|daughter|son|emr|osh)"
    r"|history (was )?(taken|obtained) from (chart|family|record)"
    r"|collateral (history|information)|per chart review|chart review"
    r"|per (osh|outside hospital) (records|notes)|per ems|per facility|per group home|per nursing (home|facility)"
    r"|poor historian|limited historian|unable to give detailed history"
    r")"
)

# 正向信号: 入院查体记录到"清醒、定向力佳", 作为可对话的佐证。
_ALERT_ORIENTED_RE = re.compile(
    r"(?i)(a&?o ?x ?3|alert and orient(ed)?( ?x ?3)?|awake,? alert)"
)


def detect_history_source(hpi: Optional[str]) -> str:
    """判定 HPI 的主要来源: patient / collateral / mixed / unknown。"""
    if not hpi:
        return "unknown"
    hard = bool(_HPI_HARD_NONCONVERSABLE_RE.search(hpi))
    coll = bool(_HPI_COLLATERAL_RE.search(hpi))
    if hard:
        return "collateral"
    if coll:
        return "mixed"
    return "patient"


def is_conversable(
    chief_complaint: Optional[str],
    hpi: Optional[str],
    physical_exam: Optional[str] = None,
) -> tuple[bool, list[str]]:
    """确定性判定病例是否"患者本人可应答问诊"。返回 (是否可对话, 命中原因列表)。

    规则:
      - 主诉命中黑名单(AMS/arrest/found down...) → 不可对话;
      - HPI 命中硬性信号(intubated&sedated / unable to provide history / GCS<8...) → 不可对话;
      - 其余视为可对话(查体 A&Ox3 仅作正向佐证, 不用于翻案排除)。
    晕厥不在黑名单内, 不会被误杀。
    """
    reasons: list[str] = []
    if chief_complaint and _CC_BLOCK_RE.search(chief_complaint):
        reasons.append(f"cc_blocklist:{_CC_BLOCK_RE.search(chief_complaint).group(0)}")
    if hpi and _HPI_HARD_NONCONVERSABLE_RE.search(hpi):
        reasons.append(
            f"hpi_nonconversable:{_HPI_HARD_NONCONVERSABLE_RE.search(hpi).group(0)[:40]}"
        )
    return (len(reasons) == 0, reasons)
