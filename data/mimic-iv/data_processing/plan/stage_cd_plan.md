# Stage C / Stage D 规划 —— 从 case JSON 到可交互的 Patient Agent

> 论文背景: *Patients Are Not Oracles: Evaluating Interactive Clinical Reasoning beyond Complete-Information QA*。
> 目标: 把 Stage A/B 产出的 case 编译成**图状态机**, 驱动一个"非神谕"的患者智能体——
> 医生必须通过多轮问诊 + 主动开检查, 逐步把隐藏诊断从候选集里收敛出来。
>
> 前置已完成:
> - **Stage A(确定性)**: 段落切分、de-id 数值回填(Bug1)、lab 白/黑名单(A3)、出院检查剔除(A4)、
>   `lab_anchor_time` + 诊断期窗口落 provenance(A5)、可对话性闸门(排除昏迷/插管/院内死亡)。
> - **Stage B(本地 LLM 闸门)**: B1 句子级病史来源、B2 病例可推理性/可解性/gold 一致性打分,
>   产出 `dataset.curated.jsonl` + `stage_b_scores.jsonl` + `flag_for_review`。
>
> 本文规划 Stage C(节点编译)与 Stage D(QC gate), 并给出 Patient Agent 的落地架构。

---

## 0. 总体数据流

```
dataset.curated.jsonl (Stage B 通过)
        │
   ┌────┴─────────────────────────────────────────┐
   │ Stage C  节点编译(scispaCy+negspaCy 主 + LLM 校验)│
   │  C1 症状/发现 → CUI + OLDCARTS 属性节点           │
   │  C2 平面归位 G_hist / G_exam / G_dx              │
   │  C3 D / D_crit / candidates(鉴别诊断候选集)      │
   └────┬─────────────────────────────────────────┘
        │  case_graph.json (每例一张图)
   ┌────┴─────────────────────────────────────────┐
   │ Stage D  QC gate(自动 + 人工)                   │
   │  D1 可解性校验(每个 D_crit 是否有可恢复来源)      │
   │  D2 admission 阈值过滤(trivial/unsolvable 踢出)  │
   │  D3 gold 子集人工 IAA                            │
   └────┬─────────────────────────────────────────┘
        │  gold/ + silver/ 两级数据集
   ┌────┴─────────────────────────────────────────┐
   │ Patient Agent / Exam Agent / Evaluator         │
   │  图状态机 + reveal policy + 指标(GVI/Coverage…)  │
   └────────────────────────────────────────────────┘
```

---

## 1. 图 schema(Patient Graph)

每个 case 编译为一张有向图 `case_graph.json`。这是 Stage C 的输出契约, 也是 Agent 的运行时状态。

### 1.1 节点(Node)

| 字段 | 说明 |
|---|---|
| `node_id` | 稳定 ID, 如 `n_hpi_003` |
| `plane` | `G_hist` / `G_exam` / `G_dx`(隐藏) |
| `kind` | `symptom` / `finding` / `vital` / `lab` / `imaging` / `micro` / `pmh` / `med` / `social` / `family` / `diagnosis` |
| `cui` | UMLS CUI(scispaCy linker), 缺失则 `null` |
| `label` | 规范名(优先 UMLS preferred term) |
| `polarity` | `present` / `absent`(negspaCy) / `uncertain` |
| `attrs` | OLDCARTS 属性: onset/location/duration/character/aggravating/relieving/timing/severity, 数值类带 `value`+`unit`+`ref_range`+`flag` |
| `source` | 来自 Stage B B1: `patient` / `collateral` / `clinician_observed` / `chart_review` / `osh_records` |
| `evidence` | 溯源指针: `{section, char_span, raw_text}`(锚回原文, 便于审计与可解性校验) |
| `reveal_cost` | reveal policy 用: 0=主诉免费, 1=问诊可得, 2=需开检查 |

### 1.2 边(Edge)

- `supports(node→diagnosis)`: 该发现支持某候选诊断(带权重, 来自 LLM 校验或知识库)。
- `requires(diagnosis→D_crit)`: 确诊该诊断的关键证据节点(定义指标分母)。
- `attribute_of(attr→symptom)`: OLDCARTS 属性挂到症状。
- `temporal(node→node)`: 时序(用于 `lab_anchor_time` 之后的趋势, 默认只暴露首测点)。

### 1.3 三平面与 reveal 语义

- **G_hist**(Patient agent 剧本): 患者入院时可知。只采纳 `source=patient`(collateral 单独保留、默认不主动吐露, 见 §4.3)。
- **G_exam**(Exam agent 环境): 查体/化验/影像/微生物。**仅在医生开具对应检查后**才 reveal。
- **G_dx**(隐藏答案): `D`(primary ICD title)、`D_crit`(确诊关键证据集合)、`candidates`(鉴别候选)。对两个 agent 全程不可见, 只给 Evaluator。

---

## 2. Stage C —— 节点编译

> 原则(承接 `data_pipline_fix.md`): **"结构/边界"用确定性方法, "语义/角色"用本地 LLM 只打标签不改写**。
> scispaCy/negspaCy 做主力抽取(确定性、可复现), 本地 LLM 仅做校验/补全/挂边。

### C1 症状/发现 → CUI + OLDCARTS 属性

输入: `history.hpi` / `pmh` / `ros`、`exams.physical_exam_admission`、结构化 `labs/radiology/micro`。

1. **实体抽取(确定性主)**: `scispaCy` (`en_core_sci_md` + `en_ner_bc5cdr_md`) 抽取 problem/test/treatment 实体。
2. **UMLS 链接**: `scispacy.linking.EntityLinker` → CUI + preferred term。结构化 labs 已带 `itemid/label`, 直接映射到 LOINC/CUI 字典(建一份 `itemid→cui` 表, 一次性离线生成)。
3. **否定/不确定**: `negspaCy`(ConText)给 `polarity`。"denies fever" → fever:absent(阴性发现对鉴别很关键, 必须保留)。
4. **OLDCARTS 属性**: 规则 + 本地 LLM 校验。规则先抽数值/时间(正则), LLM 补语义属性(character/severity), **输出受约束 JSON**, 不改原文。
5. **结构化检查直转节点**: labs/imaging/micro 已结构化, 直接成 `kind=lab/imaging/micro` 节点, 带 `value_display`(已修复 Bug1)、`flag`、`reveal_cost=2`。

产出: 每例 `nodes[]`(未归位)。

### C2 平面归位 G_hist / G_exam / G_dx

- 用 Stage A 的 `PLANE_OF_SECTION` + 节点来源决定 plane。
- **G_hist 纯净化**: 结合 Stage B B1 句子标签, 把 `source∈{chart_review,clinician_observed}` 的"发现"剔出 G_hist(它们不是患者主观可陈述的), 归到 G_exam 或丢弃。`collateral` 节点保留但打标。
- **反泄漏复核**: 任何挂在出院段/含 `lab_anchor_time` 之后时间戳的节点不得进 G_exam(沿用 A4/A5 锚点)。

### C3 D / D_crit / candidates

- `D` = `diagnosis_hidden.title`(primary ICD-10)。
- `D_crit`(确诊关键证据): **LLM 候选 + 人工终审**(半自动)。给本地 LLM:`D` + 全部 exam 节点, 让它选出"缺了它就无法确诊 D"的最小证据集, 输出 `{node_id, necessity:0-2}`。necessity=2 的进 `D_crit`。这是指标分母, 留人工抽审(接 Stage D)。
- `candidates`(鉴别集): 由 `supports` 边反推 + 知识库同科室常见鉴别, 形成 3–8 个候选, 供 Evaluator 算 MRR/排名。

产出: `output/graphs/{case_id}.graph.json` + `dataset.graph.jsonl`。

### C 实现要点

- 新增 `stage_c_compile.py`, 复用 Stage B 的 `LocalLLM` 后端抽象(`--backend heuristic|local`)。
- scispaCy 模型较大, 单独 `requirements-nlp.txt`; 在 HPC/有 GPU 环境跑, 本机用 `--backend heuristic` 做 smoke test。
- 全程缓存 CUI 链接结果(磁盘缓存), 避免重复链接。

---

## 3. Stage D —— QC gate

### D1 可解性校验(确定性集合判定)

对每个 case: `D_crit` 中每个节点是否能在 G_hist∪G_exam 中"被恢复"(医生通过问诊/开检查可达)。

- 若某 `D_crit` 节点 `reveal_cost=2` 但对应检查不在可开列表 → **unsolvable**(如胆囊炎缺 HIDA)。
- 输出 `solvable: bool` + `missing_crit: []`。与 Stage B B2 的 `solvability` 联用(B2 是粗筛, D1 是基于图的精筛)。

### D2 admission 阈值过滤

- 复用 Stage B 阈值 + D1 结果: `unsolvable` 或 B2 `trivial`(cc≈dx)直接踢出或转人工。
- 产出两级: `gold/`(全部闸门通过 + 人工确认)与 `silver/`(自动通过、未人工)。

### D3 gold 子集人工 IAA

- 抽 ~50–100 例做 gold, 2 名标注者复核 `D_crit`、`source`、`solvable`, 计算 Cohen's κ。
- Stage B 的 `flag_for_review`(low_gold_consistency / history_has_collateral)优先进人工队列。
- 工具: 复用 `export_markdown.py` 渲染 + 一个轻量标注表(node_id 勾选)。

### D 实现要点

- 新增 `stage_d_qc.py`: 读 `dataset.graph.jsonl`, 跑 D1/D2, 产出 `gold.jsonl` / `silver.jsonl` / `review_queue.jsonl`。

---

## 4. Patient Agent 架构(图状态机)

### 4.1 状态机

- **State** = 已 reveal 节点集合 + 对话历史 + 已开检查集合。
- **Action(医生)**: `ask(question)` | `order(exam)` | `commit(diagnosis)`。
- **Transition**:
  - `ask` → Patient agent 在 G_hist 中检索匹配节点并以患者口吻回答; 未问到的不主动给(非神谕核心)。
  - `order` → Exam agent reveal 对应 G_exam 节点(只给 `lab_anchor_time` 前的首测点)。
  - `commit` → 终止, 交 Evaluator。

### 4.2 Reveal Policy(非神谕的关键)

- 主诉(`reveal_cost=0`)开局即给。
- 问诊命中(语义匹配 question↔node)才 reveal `cost=1` 节点; 阴性发现按需回答("有没有发烧?"→denies fever)。
- 检查节点 `cost=2` 必须 `order` 后才给, 且受"可开检查表"约束(D1 校验过)。
- **禁止泄漏**: G_dx 永不可见; 未问到的 G_hist 不吐; 出院后证据不进环境。

### 4.3 患者人格 / 健康素养先验(承接此前年龄讨论)

- 用 `demographics.age/language/marital_status` 设 persona 先验: 高龄/低素养 → 更口语化、可能漏报术语、需追问; 这是**特性不是 bug**(医生问诊能力的一部分)。
- `collateral` 节点: 默认患者不主动说"我女儿说…", 但当医生明确问到家属信息时可吐露并标 source。

### 4.4 忠实性约束(防 LLM 患者乱编)

- Patient agent 回答**必须可溯源到节点 `evidence`**; 用受约束生成(只能引用图内节点)+ 事后校验(回答里的临床事实须在 G_hist 命中, 否则判 hallucination)。
- 不确定/不知道时显式说"不清楚", 不得编造数值。

---

## 5. Exam Agent + Evaluator(简述)

- **Exam Agent**: 把 `order(exam)` 映射到 G_exam 节点, 处理"开了但本例没有该检查"→返回"未做/无结果", 并按 §1.2 `temporal` 只给首测点 + 出报时延标签。
- **Evaluator(用 G_dx)**:
  - **GVI (Gap from Verified Information)**: full-info(给完整图)vs interactive(需问诊)下的诊断差距。
  - **Coverage**: 医生收集到的 `D_crit` 比例。
  - **Early Closure**: 是否在未覆盖关键鉴别证据前过早 `commit`。
  - **MRR / rank**: `commit` 诊断在 `candidates` 上的排名。
- 指标分母(`D_crit`、`candidates`)由 Stage C3 + Stage D 锁定, 保证可复现。

---

## 6. 落地路线图

| 步骤 | 文件 | 后端 | 产物 |
|---|---|---|---|
| C1–C3 | `stage_c_compile.py` + `requirements-nlp.txt` | scispaCy/negspaCy + 本地 LLM 校验 | `graphs/*.graph.json` |
| D1–D3 | `stage_d_qc.py` | 确定性 + 人工 | `gold.jsonl` / `silver.jsonl` / `review_queue.jsonl` |
| Agent | `agents/patient_agent.py` / `exam_agent.py` / `evaluator.py` | 本地/被测 LLM | 交互 trace + 指标 |
| 标注 | 复用 `export_markdown.py` | 人工 | IAA(κ) |

**建议顺序**: 先 C1+C2(确定性抽取 + 平面归位, 本机可 smoke)→ C3 `D_crit`(需本地 LLM, 上 HPC)→ D1/D2 自动闸门 → 抽 gold 跑 D3 → 再搭 Agent。

---

## 7. 合规与可复现(全程遵守)

- 所有语义打分/校验**只用本地模型**(Stage B/C 的 `--backend local` 指向本地端点), 绝不把 MIMIC 文本送外部 API。
- LLM 一律 `temperature=0` + 受约束 JSON, 只打标签不改写原文; 启发式后端可在无模型环境复跑做基线对照。
- 每例保留 `provenance`(窗口/锚点/过滤规则)与节点 `evidence`(原文 span), 论文里能用一段话把全部过滤规则写清。
- `output/` 已在 `.gitignore`, 不提交任何 MIMIC 派生数据。
