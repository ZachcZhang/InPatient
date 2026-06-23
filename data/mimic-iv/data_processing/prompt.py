"""Stage B 闸门提示词(B1 病史来源 / B2 病例准入)。

设计原则(承接 data_pipline_fix.md):
  - LLM 只"打分/打标签", 绝不改写或生成原文; temperature=0, 输出受约束 JSON。
  - 每个分数都给出**可操作的 0-5 评分标准(rubric)**, 降低主观性、提高 IAA 与可复现性。
  - 评审视角: 服务于论文 *Patients Are Not Oracles* —— 拦掉"主诉≈答案"的退化病例,
    以及病史非患者本人提供/证据不足无法推理的病例。
"""

from __future__ import annotations

import json
from typing import Dict, List

# =========================================================================== #
# B1  Source Attribution Scorer —— HPI 逐句病史来源
# =========================================================================== #
B1_SYSTEM = """You are a clinical NLP annotator for a patient-simulator benchmark.
TASK: For EACH numbered HPI sentence, label who the information primarily came from.
You only assign labels; you MUST NOT rewrite, summarize, translate, or invent text.

SOURCE LABELS (choose exactly one per sentence):
- patient            : the patient themself reported it (e.g., "reports", "denies", "states", "complains of", first-person symptom history).
- collateral         : a family member / caregiver / bystander provided it (e.g., "per his wife", "family reports", "per group home staff").
- chart_review       : taken from prior records/EMR/PCP notes (e.g., "per chart", "review of records", "per OMR", "history per prior notes").
- clinician_observed : an objective finding observed/measured by staff, not patient-reported (e.g., "on exam", "vitals were", "labs showed", "CT revealed", "found unresponsive on arrival").
- osh_records        : from an outside/transferring hospital (e.g., "transferred from OSH", "per outside facility records").

GUIDANCE:
- Default to `patient` only when the sentence is clearly the patient's own account.
- If a sentence mixes sources, pick the PRIMARY source of the clinical content.
- `confidence` in [0,1]: use <0.5 when genuinely ambiguous (these go to human review).

OUTPUT: strict JSON only, no prose:
{"sentences":[{"id":<int>,"source":"<label>","confidence":<float>}]}"""


def b1_user(sentences: List[str]) -> str:
    body = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences))
    return (
        "Label the SOURCE of each HPI sentence below.\n\nSENTENCES:\n"
        + body
        + '\n\nReturn JSON: {"sentences":[{"id":int,"source":str,"confidence":float}]}'
    )


# =========================================================================== #
# B2  Case Admission Scorer —— 病例可推理性 / 可解性 / gold 一致性
# =========================================================================== #
B2_SYSTEM = """You are a senior clinical case auditor curating a benchmark that evaluates
INTERACTIVE diagnostic reasoning (a doctor must ask history + order exams to converge on a
hidden diagnosis). You score one case. You MUST NOT use hindsight: ignore treatment response,
operative findings, or anything that would only be known AFTER the diagnosis is established.
You only output scores and short reasons; never rewrite clinical text.

Return STRICT JSON ONLY:
{
  "diagnostic_reasoning_score": int 0-5,
  "cc_dx_distance": int 0-5,
  "gold_consistency": int 0-5,
  "solvability": bool,
  "recommendation": "admit" | "review" | "reject",
  "reasons": [string, ...]
}

================= RUBRICS (apply literally) =================

A) diagnostic_reasoning_score — how much genuine interactive reasoning is needed to reach the
   gold diagnosis from the presentation. Higher = better benchmark case.
   0 = Degenerate: the diagnosis IS the presenting symptom / chief complaint restates the answer
       (e.g., CC "chest pain" -> dx "Chest pain, unspecified"). No reasoning, no differential.
   1 = Trivial: diagnosis is obvious from the chief complaint alone; no workup needed.
   2 = Low: largely obvious; only a single confirmatory test stands between CC and dx.
   3 = Moderate: a small differential (2-4) that standard history + routine workup resolves.
   4 = High: broad differential requiring integration of history + multiple exam modalities to converge.
   5 = Rich: non-obvious diagnosis requiring careful multi-step reasoning across history and exams;
       an exemplary case for the benchmark.

B) cc_dx_distance — semantic distance between the chief complaint and the gold diagnosis.
   0 = Identical (CC == dx wording).
   1 = CC is the defining symptom / near-synonym of dx.
   2 = CC strongly implies dx; very short differential.
   3 = CC related to dx but several organ systems plausible.
   4 = CC broad / nonspecific relative to dx.
   5 = CC and dx appear unrelated -> likely mislabeled gold or non-patient pathway (FLAG).

C) gold_consistency — agreement among primary ICD title, discharge-diagnosis text, and ED diagnoses.
   Judge clinical equivalence, NOT string overlap (e.g., "NSTEMI" == "Non-ST elevation MI").
   0 = Contradictory (different organ systems / mutually exclusive).
   1 = Largely inconsistent.
   2 = Partial overlap with notable divergence.
   3 = Mostly consistent; minor wording/coding/granularity differences.
   4 = Consistent; same entity, different specificity.
   5 = Fully consistent across all sources.
   If only one gold source is available, score 3 and note it.

D) solvability (bool) — Given ONLY the patient-knowable history plus exams a doctor could ORDER
   during the diagnostic window (no hindsight), is there enough RECOVERABLE evidence to reach the
   gold diagnosis? true if a competent clinician could get there; false if the key confirmatory
   evidence is absent/unrecoverable from this case (e.g., the defining test was never done/recorded).

E) recommendation:
   - "reject"  if diagnostic_reasoning_score <= 1 OR cc_dx_distance == 0 OR solvability == false.
   - "review"  if gold_consistency <= 1 OR cc_dx_distance == 5 OR any score is borderline/uncertain.
   - "admit"   otherwise.

reasons: 1-3 short bullet strings citing the concrete trigger(s). Be specific and terse."""


def b2_user(case: Dict) -> str:
    dx = case.get("diagnosis_hidden", {}) or {}
    exams = case.get("exams", {}) or {}
    payload = {
        "chief_complaint": case.get("presentation", {}).get("chief_complaint"),
        "hpi": case.get("history", {}).get("hpi"),
        "n_labs": len(exams.get("labs", []) or []),
        "n_radiology": len(exams.get("radiology", []) or []),
        "n_micro": len(exams.get("microbiology", []) or []),
        "primary_dx_title": dx.get("title"),
        "discharge_dx_text": dx.get("discharge_diagnosis_text"),
        "ed_dx": [e.get("icd_title") for e in (dx.get("ed_diagnosis") or [])],
    }
    return (
        "Score this case per the rubrics. Output strict JSON only.\n\nCASE:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
