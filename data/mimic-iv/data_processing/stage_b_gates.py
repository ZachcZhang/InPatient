"""Stage B —— 本地 LLM 质量闸门(只打分/打标签, 绝不改写原文)。

两个闸门, 产出写进每个 case 的 `qc` 块, 并据阈值产出 curated 数据集:

  B1  Source Attribution Scorer
      对 HPI 逐句判定信息来源 ∈ {patient, collateral, chart_review,
      clinician_observed, osh_records}, 给出 source_patient_ratio。
      → 守住 patient simulator 前提: G_hist 理应只采纳 source=patient 的内容
        (实际筛选在 Stage C 落地, 这里先打标)。

  B2  Case Admission Scorer
      对每个 case 输出 {diagnostic_reasoning_score, cc_dx_distance,
      gold_consistency, solvability, reasons}。
      → 拦掉两端: 主诉≈答案的 trivial 病例(GVI≈0, 稀释指标), 以及
        gold 自相矛盾 / 证据不足无法推理的病例。

提示词与评分标准(rubric)集中在 prompt.py。

合规: `--backend heuristic`(默认)纯规则, 无任何外部调用, 可直接跑;
      `--backend local` 调用 .env(API_KEY/MODEL/BASE_URL)配置的 OpenAI 兼容端点
      (temp=0, 受约束 JSON, 只打分不改写)。
      ⚠ DUA: 把凭证版 MIMIC 文本发往非本地第三方 API 违反 PhysioNet DUA。脚本对外部端点
      默认中止, 仅在确认为 demo 开放数据时加 --allow-external。生产应指向本地模型端点。

用法:
  python stage_b_gates.py                         # 启发式打分 + 过滤
  python stage_b_gates.py --backend local         # 读取 .env 的 BASE_URL/MODEL/API_KEY
  python stage_b_gates.py --backend local --allow-external   # 仅 demo 开放数据
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import prompt as P

# --------------------------------------------------------------------------- #
# .env 加载(API_KEY / MODEL / BASE_URL)
# --------------------------------------------------------------------------- #
def load_env(start: Optional[Path] = None) -> Dict[str, str]:
    """从项目目录向上查找 .env, 解析 KEY=VALUE。已存在的环境变量优先。"""
    env: Dict[str, str] = {}
    here = (start or Path(__file__).resolve()).parent
    for d in [here, *here.parents]:
        candidate = d / ".env"
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
            break
    # 真实环境变量覆盖文件值
    for k in ("API_KEY", "MODEL", "BASE_URL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
_STOP = set(
    """unspecified acute chronic of with and the other due to initial episode care
    site type disease disorder nos without not a an in on for by as is at""".split()
)


def _tokens(text: Optional[str]) -> set:
    if not text:
        return set()
    toks = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in toks if len(t) > 2 and t not in _STOP}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _clamp05(x, default: int = 0) -> int:
    """把 LLM 返回的分数强制到 [0,5] 整数; 无法解析时给 default。"""
    try:
        return max(0, min(5, int(round(float(x)))))
    except (TypeError, ValueError):
        return default


def _sentences(text: Optional[str]) -> List[str]:
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [s.strip() for s in parts if len(s.strip()) >= 8]


# =========================================================================== #
# B1 启发式: HPI 句子来源归属
# =========================================================================== #
_ROLE_CUES = {
    "osh_records": re.compile(
        r"(?i)\b(osh|outside hospital|transferred from|per facility|per group home|per nursing)\b"
    ),
    "chart_review": re.compile(
        r"(?i)(per (the )?chart|chart review|per (the )?(medical )?record|per omr|"
        r"review of (records|notes)|per (prior|ed|er) notes|per emr)"
    ),
    "collateral": re.compile(
        r"(?i)(per (his|her|the) (wife|husband|family|daughter|son|mother|father|"
        r"partner|caregiver|spouse|sister|brother)|from (his|her|the) family|collateral)"
    ),
    "clinician_observed": re.compile(
        r"(?i)(on exam|on arrival|in the ed|on presentation|found to be|noted to be|"
        r"vitals were|labs (showed|notable)|ct (showed|revealed)|was given|"
        r"triage|exam notable|imaging (showed|revealed))"
    ),
    "patient": re.compile(
        r"(?i)(reports|complains|c/o|states|denies|endorses|describes|notes that|"
        r"per patient|patient (reports|states|notes|feels)|she (reports|states)|"
        r"he (reports|states))"
    ),
}
_ROLE_PRIORITY = ["osh_records", "chart_review", "collateral", "clinician_observed", "patient"]


def b1_heuristic(hpi: Optional[str]) -> dict:
    sents = _sentences(hpi)
    labeled = []
    counts: Dict[str, int] = {}
    for s in sents:
        role = "patient"  # 默认按患者陈述
        for r in _ROLE_PRIORITY:
            if _ROLE_CUES[r].search(s):
                role = r
                break
        counts[role] = counts.get(role, 0) + 1
        labeled.append({"text": s[:200], "source": role, "confidence": 0.6})
    n = max(len(sents), 1)
    ratio = counts.get("patient", 0) / n
    return {
        "source_patient_ratio": round(ratio, 3),
        "source_counts": counts,
        "n_sentences": len(sents),
        "sentences": labeled,
    }


# =========================================================================== #
# B2 启发式: 病例可推理性 / 可解性 / gold 一致性
# =========================================================================== #
def b2_heuristic(case: dict) -> dict:
    pres = case.get("presentation", {})
    dx = case.get("diagnosis_hidden", {})
    exams = case.get("exams", {})
    hist = case.get("history", {})

    cc = pres.get("chief_complaint") or ""
    gold_title = dx.get("title") or ""
    dc_text = dx.get("discharge_diagnosis_text") or ""
    ed_titles = " ".join(e.get("icd_title") or "" for e in (dx.get("ed_diagnosis") or []))

    cc_tok = _tokens(cc)
    gold_tok = _tokens(gold_title)

    # cc 与 gold 的重合度 → cc_dx_distance(0=主诉即答案, 5=完全不相关)
    overlap = _jaccard(cc_tok, gold_tok)
    cc_dx_distance = round((1 - overlap) * 5)

    n_labs = len(exams.get("labs", []))
    n_rad = len(exams.get("radiology", []))
    n_micro = len(exams.get("microbiology", []))
    has_workup = n_labs >= 5 or n_rad >= 1 or n_micro >= 1
    hpi_len = len(hist.get("hpi") or "")

    # diagnostic_reasoning_score(0-5): 主诉越接近答案越低; 有工作期检查则加分
    if overlap >= 0.5:
        reasoning = 1
    elif overlap >= 0.34:
        reasoning = 2
    else:
        reasoning = 4 if has_workup else 3
    if not has_workup:
        reasoning = min(reasoning, 2)

    # gold_consistency(0-5): primary title / discharge dx / ed dx 三者重合度
    consist_pairs = [
        _jaccard(gold_tok, _tokens(dc_text)),
        _jaccard(gold_tok, _tokens(ed_titles)),
    ]
    consist_pairs = [c for c in consist_pairs if c > 0]
    gold_consistency = round((sum(consist_pairs) / len(consist_pairs)) * 5) if consist_pairs else 3

    solvability = has_workup and hpi_len >= 200 and reasoning >= 2

    reasons = []
    if overlap >= 0.5:
        reasons.append("cc≈dx(trivial: 主诉基本点明诊断)")
    if not has_workup:
        reasons.append("无工作期检查证据(labs/影像/micro 不足)")
    if gold_consistency <= 1:
        reasons.append("gold 口径不一致(primary/discharge/ed dx 分歧)")

    return {
        "cc_dx_distance": cc_dx_distance,
        "diagnostic_reasoning_score": reasoning,
        "gold_consistency": gold_consistency,
        "solvability": bool(solvability),
        "n_labs": n_labs,
        "n_radiology": n_rad,
        "n_micro": n_micro,
        "reasons": reasons,
    }


# =========================================================================== #
# B3 泄漏审计(确定性, 无外部依赖)
#
# 扫描"暴露给医生的平面"是否泄漏了隐藏答案或事后信息:
#   - history_dx_leak : gold 诊断词出现在患者**病史**(HPI/ROS) → 真泄漏(患者不该自己说出诊断)
#   - exam_dx_mention : gold 诊断词出现在**检查**自由文本(查体/结果/影像) → 信息项, 非泄漏
#                       (影像 impression 点名诊断是临床常态, 且受"开检查"门控; 但提示该检查可能即 D_crit)
#   - tx_leak         : 治疗/转归(post-op / s/p ...ectomy / started on / path showed ...)出现在暴露平面
#                       → 事后信息泄漏(诊断期不应可见), 转人工
# =========================================================================== #
# ICD 修饰词 + 解剖/泛化词(非特异性诊断词), 从 gold 关键词中剔除以降假阳。
_GOLD_TERM_STOP = set(
    """acute chronic unspecified other others initial subsequent sequela encounter episode
    disease diseases disorder disorders syndrome syndromes condition type types with without
    due and the of nos history status post complication complications finding findings abnormal
    abnormality elevated decreased increased left right bilateral lower upper severe mild moderate
    primary secondary stage grade site unspec uns late effect part organism
    artery arteries arterial vein veins venous coronary pulmonary cardiac heart renal kidney
    kidneys hepatic liver chest abdominal abdomen urinary gastric gastrointestinal cerebral
    vascular thoracic lumbar bowel lung lungs vessel vessels tissue muscle bone joint organ
    system region tract distal proximal anterior posterior native""".split()
)
_DISEASE_SUFFIX_RE = re.compile(r"(itis|emia|aemia|osis|pathy|oma|algia|plegia|ectasis)$", re.I)
_TX_LEAK_RE = re.compile(
    r"(?i)\b("
    r"post-?op(erative)?|intra-?op(erative)?|peri-?op(erative)?"
    r"|tolerated the procedure|was (started|treated) (on|with)"
    r"|path(ology)?\s+(showed|revealed|demonstrated|confirmed)"
    r"|drain (was )?placed|stent (was )?placed"
    r"|\w+(ectomy|ostomy)\b"
    r")\b"
)


def _gold_terms(case: dict) -> List[str]:
    # 只用 primary title(隐藏答案), 不用 discharge_diagnosis_text(列了全部合并症, 易误判)
    title = (case.get("diagnosis_hidden", {}) or {}).get("title") or ""
    terms = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", title.lower()):
        if tok in _GOLD_TERM_STOP:
            continue
        if len(tok) >= 5 or _DISEASE_SUFFIX_RE.search(tok):
            terms.add(tok)
    return sorted(terms)


def _find_terms(text: Optional[str], terms: List[str]) -> List[dict]:
    if not text:
        return []
    low = text.lower()
    hits = []
    for t in terms:
        m = re.search(r"\b" + re.escape(t) + r"\b", low)
        if m:
            s, e = max(0, m.start() - 40), min(len(text), m.end() + 40)
            hits.append({"term": t, "snippet": text[s:e].replace("\n", " ").strip()})
    return hits


def _find_tx(text: Optional[str]) -> List[dict]:
    if not text:
        return []
    out = []
    for m in _TX_LEAK_RE.finditer(text):
        s, e = max(0, m.start() - 30), min(len(text), m.end() + 30)
        out.append({"pattern": m.group(0), "snippet": text[s:e].replace("\n", " ").strip()})
    return out[:10]


def b3_leakage_audit(case: dict) -> dict:
    hist = case.get("history", {}) or {}
    exams = case.get("exams", {}) or {}
    # 病史(患者可自述, 不含 chief_complaint —— 其 trivial 问题由 cc_dx_distance 负责)
    hist_text = " ".join(filter(None, [hist.get("hpi"), hist.get("ros")]))
    # 检查自由文本(开检查后才可得)
    exam_text = " ".join(
        filter(
            None,
            [
                exams.get("physical_exam_admission"),
                exams.get("pertinent_results_text"),
                " ".join((r.get("text") or "") for r in (exams.get("radiology") or [])),
            ],
        )
    )
    terms = _gold_terms(case)
    # 主诉里出现的 gold 词 = 患者的"就诊症状", 不算泄漏(其 trivial 性由 cc_dx_distance 负责)
    cc = (case.get("presentation", {}) or {}).get("chief_complaint") or ""
    cc_low = cc.lower()
    hist_terms = [t for t in terms if t not in cc_low]
    history_dx_leak = _find_terms(hist_text, hist_terms)
    exam_dx_mention = _find_terms(exam_text, terms)
    tx_leak = [{**h, "where": "history"} for h in _find_tx(hist_text)] + [
        {**h, "where": "exam"} for h in _find_tx(exam_text)
    ]
    return {
        "gold_terms": terms,
        "history_dx_leak": history_dx_leak,   # 危险: 病史泄漏诊断名
        "exam_dx_mention": exam_dx_mention,   # 信息: 哪个检查点名诊断(候选 D_crit)
        "tx_leak": tx_leak,                   # 事后治疗/转归泄漏
        "has_history_dx_leak": bool(history_dx_leak),
        "has_tx_leak": bool(tx_leak),
    }


# =========================================================================== #
# 本地 LLM 后端(可选; 走本地 OpenAI 兼容端点)
# =========================================================================== #
class LocalLLM:
    """OpenAI 兼容端点封装(temp=0, 受约束 JSON)。提示词见 prompt.py。

    端点由 .env 的 BASE_URL/MODEL/API_KEY 提供。注意: 若 BASE_URL 非本地主机,
    把 MIMIC 文本发往外部 API 会触犯 PhysioNet DUA(仅 demo 开放数据可外发)。
    """

    def __init__(self, base_url: str, model: str, api_key: str = "local"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def _chat(self, system: str, user: str) -> dict:
        import urllib.request

        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)

    def b1(self, hpi: str) -> dict:
        sents = _sentences(hpi)
        try:
            out = self._chat(P.B1_SYSTEM, P.b1_user(sents))
            labeled = out.get("sentences", [])
            counts: Dict[str, int] = {}
            for it in labeled:
                src = it.get("source", "patient")
                counts[src] = counts.get(src, 0) + 1
            n = max(len(sents), 1)
            return {
                "source_patient_ratio": round(counts.get("patient", 0) / n, 3),
                "source_counts": counts,
                "n_sentences": len(sents),
                "sentences": labeled,
            }
        except Exception as e:  # noqa: BLE001  端点不可用时回退启发式
            r = b1_heuristic(hpi)
            r["llm_error"] = str(e)
            return r

    def b2(self, case: dict) -> dict:
        try:
            out = self._chat(P.B2_SYSTEM, P.b2_user(case))
            return {
                "cc_dx_distance": _clamp05(out.get("cc_dx_distance")),
                "diagnostic_reasoning_score": _clamp05(out.get("diagnostic_reasoning_score")),
                "gold_consistency": _clamp05(out.get("gold_consistency"), default=3),
                "solvability": bool(out.get("solvability")),
                "recommendation": out.get("recommendation", "review"),
                "reasons": out.get("reasons", []) or [],
            }
        except Exception as e:  # noqa: BLE001
            r = b2_heuristic(case)
            r["llm_error"] = str(e)
            return r


# =========================================================================== #
# 主流程
# =========================================================================== #
def run(rows: List[dict], backend, args) -> Tuple[List[dict], List[dict]]:
    scores: List[dict] = []
    curated: List[dict] = []
    for case in rows:
        hpi = case.get("history", {}).get("hpi")
        if backend is None:
            b1 = b1_heuristic(hpi)
            b2 = b2_heuristic(case)
        else:
            b1 = backend.b1(hpi or "")
            b2 = backend.b2(case)

        leak = b3_leakage_audit(case)

        admitted = (
            b2["diagnostic_reasoning_score"] >= args.min_reasoning
            and b2["solvability"]
            and b2["cc_dx_distance"] >= args.min_cc_dx_distance
            and b2["gold_consistency"] >= args.min_gold_consistency
            and b1["source_patient_ratio"] >= args.min_patient_ratio
        )
        reasons = list(b2.get("reasons", []))
        if b1["source_patient_ratio"] < args.min_patient_ratio:
            reasons.append(
                f"病史多为旁人/病历代述(patient_ratio={b1['source_patient_ratio']})"
            )

        # B3: 病史泄漏诊断名 —— 默认转人工; --reject-on-leakage 时硬拒绝
        if leak["has_history_dx_leak"] and args.reject_on_leakage:
            admitted = False
            terms = ",".join(h["term"] for h in leak["history_dx_leak"])
            reasons.append(f"病史泄漏诊断名({terms})")

        # gold 一致性低 / 病史含旁人代述 / 泄漏 → 通过但标记转人工复核(不自动剔除)
        flags = []
        if b2["gold_consistency"] <= 1:
            flags.append("low_gold_consistency")
        if b1["source_patient_ratio"] < 0.6:
            flags.append("history_has_collateral")
        if leak["has_history_dx_leak"]:
            flags.append("dx_leak_in_history")
        if leak["has_tx_leak"]:
            flags.append("treatment_leak")

        qc = {
            "backend": "heuristic" if backend is None else "local-llm",
            "b1_source": b1,
            "b2_admission": b2,
            "b3_leakage": leak,
            "admitted": bool(admitted),
            "reject_reasons": [] if admitted else reasons,
            "flag_for_review": flags,
        }
        scores.append({"case_id": case["case_id"], **qc})
        if admitted:
            c = dict(case)
            c["qc"] = qc
            curated.append(c)
    return scores, curated


def parse_args(argv=None):
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", default=str(here / "output" / "dataset.jsonl"))
    ap.add_argument("--out-dir", default=str(here / "output"))
    ap.add_argument(
        "--backend",
        choices=["heuristic", "local"],
        default="heuristic",
        help="heuristic=纯规则无外部调用; local=调用 .env 配置的 LLM 端点",
    )
    ap.add_argument("--llm-base-url", default=None, help="覆盖 .env 的 BASE_URL")
    ap.add_argument("--llm-model", default=None, help="覆盖 .env 的 MODEL")
    ap.add_argument(
        "--allow-external",
        action="store_true",
        help="允许把数据发往非本地端点(DUA: 仅 demo 开放数据可外发, 凭证数据严禁)",
    )
    # 准入阈值
    ap.add_argument("--min-reasoning", type=int, default=2, help="diagnostic_reasoning_score 下限")
    ap.add_argument("--min-cc-dx-distance", type=int, default=1, help="cc_dx_distance 下限(防 trivial)")
    ap.add_argument(
        "--min-gold-consistency",
        type=int,
        default=0,
        help="gold_consistency 硬下限(默认 0=仅记录不拒绝; gold 冲突应转人工而非自动剔除)",
    )
    ap.add_argument("--min-patient-ratio", type=float, default=0.4, help="HPI 患者来源句占比下限")
    ap.add_argument(
        "--reject-on-leakage",
        action="store_true",
        help="病史泄漏诊断名时硬拒绝(默认仅标记 dx_leak_in_history 转人工)",
    )
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    jsonl = Path(args.jsonl)
    if not jsonl.exists():
        print(f"[ERROR] 找不到 {jsonl}, 请先运行 build_dataset.py")
        return 1
    rows = [json.loads(l) for l in jsonl.open(encoding="utf-8")]

    backend = None
    if args.backend == "local":
        env = load_env()
        base_url = args.llm_base_url or env.get("BASE_URL")
        model = args.llm_model or env.get("MODEL")
        api_key = env.get("API_KEY", "local")
        if not base_url or not model:
            print("[ERROR] 未找到 BASE_URL/MODEL, 请在项目 .env 配置或用 --llm-base-url/--llm-model")
            return 1

        # DUA 合规闸: 非本地端点默认拒绝(凭证版 MIMIC 文本严禁外发)
        host = re.sub(r"^https?://", "", base_url).split("/")[0].split(":")[0]
        is_local = host in ("127.0.0.1", "localhost", "0.0.0.0") or host.endswith(".local")
        if not is_local:
            print(
                f"[警告] BASE_URL={base_url} 为外部端点。把凭证版 MIMIC 文本发往第三方 API "
                f"违反 PhysioNet DUA(仅 demo 开放数据可外发)。"
            )
            if not args.allow_external:
                print("[ERROR] 已中止。确认数据为 demo 开放数据后, 加 --allow-external 再跑。")
                return 1
        backend = LocalLLM(base_url, model, api_key)
        print(f"[stage-b] 使用 LLM 端点 {base_url} (model={model})")
    else:
        print("[stage-b] 使用启发式后端(无外部调用)")

    scores, curated = run(rows, backend, args)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "stage_b_scores.jsonl").open("w", encoding="utf-8") as f:
        for s in scores:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with (out / "dataset.curated.jsonl").open("w", encoding="utf-8") as f:
        for c in curated:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    n = len(rows)
    adm = len(curated)
    rej = n - adm
    import collections

    rej_reasons = collections.Counter()
    flag_counts = collections.Counter()
    for s in scores:
        for fl in s.get("flag_for_review", []):
            flag_counts[fl] += 1
        if not s["admitted"]:
            for r in s["reject_reasons"]:
                rej_reasons[r.split("(")[0]] += 1
    summary = {
        "n_input": n,
        "n_admitted": adm,
        "n_rejected": rej,
        "reject_reason_counts": dict(rej_reasons),
        "flag_for_review_counts": dict(flag_counts),
        "thresholds": {
            "min_reasoning": args.min_reasoning,
            "min_cc_dx_distance": args.min_cc_dx_distance,
            "min_gold_consistency": args.min_gold_consistency,
            "min_patient_ratio": args.min_patient_ratio,
            "reject_on_leakage": args.reject_on_leakage,
        },
    }
    with (out / "stage_b_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[stage-b] 输入 {n} → 通过 {adm} / 拒绝 {rej}")
    print(f"[stage-b] 拒绝原因: {dict(rej_reasons)}")
    print(f"[stage-b] 转人工标记: {dict(flag_counts)}")
    print(f"[done] → {out/'dataset.curated.jsonl'} , {out/'stage_b_scores.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
