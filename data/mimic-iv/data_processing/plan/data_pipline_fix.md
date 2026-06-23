## 个人思考修复
1.过滤重症昏迷 / 无意识患者
当前 cohort 没有任何意识过滤，而且我没有排除院内死亡(hospital_expire_flag 只选了未用作过滤)——院内死亡里有相当比例是持续恶化/无意识者，建议一并处理。
推荐的过滤手段（按性价比排序，可叠加）：
1. 病史来源检测（最强、最对题）：MIMIC 的 HPI 经常显式写明信息来源。检测如下短语即判为"非患者本人提供" → 排除或打 history_source=collateral 标记：
  - unable to provide history / history obtained from family/chart / per family / collateral / nonverbal / intubated / sedated / unresponsive / found down / obtunded / GCS。
  - 这条逻辑直接守住了"patient simulator 的前提"，对评审最好辩护。
2. 主诉黑名单：对 triage.chiefcomplaint 和 note 的 CC 过滤掉 altered mental status / unresponsive / cardiac arrest / coma / unconscious / s/p arrest。
3. 诊断类别排除：主诊断落在昏迷(R40.2x)、心脏骤停(I46.x)、需机械通气的呼衰、癫痫持续状态、重度卒中等 → 视为 cohort 设计排除（注意这是隐藏答案，用作"选案"是允许的）。
4. 正向信号兜底：入院查体含 alert and oriented ×3 / A&Ox3 作为"可对话"的正向确认。
建议落地为 note_sections 里一个 is_conversable(case) 判定，输出 history_source 字段并在 cohort/assemble 阶段过滤，可复现、可在论文里报一行过滤规则。晕厥不要误杀。
2. 住院时间设置为72h排除窗口，将exam检查 纳入阶段各自落地，用在手册确定确定性治疗时间前，（取该时刻前的检查）。
- 给 Exam agent 的检查项打 latency 标签（physical/lab/imaging/micro + 出报时延）
- 锦上添花：若能近似"首次确定性治疗/操作时间"作为硬上界（取该时刻前的检查），比纯时间窗更贴"诊断期"语义，但实现成本较高，可作为 v2。

## 数据pipline修复
结构 schema 已经成型且合理(presentation/history/exams/diagnosis_hidden/provenance 分区干净,labs 已结构化带 itemid),但几个静默 bug 正在产生错误数据,而且这个 case 恰好暴露了一个你的整套抽取逻辑都绕不开的根本问题。我先把数据格式问题按严重度列清楚,再回答你最关心的"哪些节点该上 LLM"。
一、必须修的数据格式 bug(按严重度)
Bug 1(最严重,静默出错):de-id 回填逻辑反了或缺失,导致 value 与 valuenum 矛盾。
 看 Glucose 这条:
json
{"label":"Glucose","value":"___","valuenum":119.0,"ref_range_upper":100.0,"flag":"abnormal"}
value 还是 ___,但 valuenum 是 119.0——可你的 markdown 里 Glucose 显示 ___ mg/dL,而 Pertinent Results 文本里写的是 Glucose-111*。三处三个状态(___ / 119 / 111),没有一个自洽。 119 不知从哪来(可能是另一时间点的值被错填),markdown 又只信了 value 字段没用 valuenum。这说明:①结构表和文本的交叉回填没做;②markdown 渲染读的是 value 而非 valuenum,导致明明有数也显示成 ___。这是会污染 exam plane 的硬 bug,必须先修。
Bug 2(数据不一致,影响 D_crit 可信度):同一指标两个时间点的值在 JSON 和文本间打架。
 JSON labs 里 Hgb=9.4、Hct=31.4、WBC=1.9、Plt=124;但 Pertinent Results 文本(admission labs)是 Hgb-9.9、Hct-33.6、WBC-2.9、Plt 未显示。你的结构化 labs 取的是 2198-04-18 03:06(入院次日凌晨),文本取的是入院当天 03:27PM——两套不同采血时间。这本身不算错,但你得显式决定:D_crit 用哪一个时间点? 现在 JSON 默默选了较晚那次(WBC 从 2.9 掉到 1.9,leukopenia 更明显)。这种选择必须写进 provenance(比如 lab_anchor_time),否则不可复现,而且 WBC 2.9 vs 1.9 会影响"白细胞减少"这个节点的认定。
Bug 3(分诊体征自相矛盾):presentation 里 triage SBP/DBP=150/98,但同一段 HPI 和 admission exam 的 vitals 又各不相同。 这个不算 bug(不同测量时点正常),但要注意 triage vitals 是医生 intake 唯一可见的,别和 admission exam vitals 混用。当前看是分开的,OK,保持。
Bug 4(垃圾行/缺失这次反而干净): 这个 case 的 labs 没有上两例的 H/I/L、HPE* 垃圾行——说明你要么这次源数据干净,要么加了过滤。确认一下是不是加了 itemid 白名单;如果是源数据偶然干净,那过滤逻辑还没真正生效,换个 case 还会冒出来。

二、这个 case 暴露的根本问题:它不该进数据集
这比格式 bug 更重要。这个 case 的诊断任务本身是退化的。 主诉直接就是 alcohol intoxication,金标准是 F10239 酒精依赖伴戒断——主诉≈答案,不存在"通过问诊+检查逐步缩小鉴别诊断"的过程。你的整个框架(GVI、Coverage、Early Closure、MRR gap)建立在"诊断需要推理"之上;一个 chief complaint 已经点明诊断的 case,full-info 和 constrained 条件下 MRR 都接近 1,GVI≈0,这个 case 对你的核心假设零贡献,还会稀释指标。
而且它的 gold 本身是混乱的:primary ICD 是"酒精戒断",discharge text 写"polysubstance abuse",ED dx 还有"白细胞增多"和"其他精神障碍"——三个口径不一致,且都是 F/Z 类精神-行为诊断,不是诊断推理谜题。
所以真正要优化的不是把这个 case 修好,而是在 pipeline 里加一道"诊断可推理性"准入筛。 这正好接上你上次留的可解性 QC,但方向相反:上个 case(胆囊炎)是 D_crit 节点缺失导致 unsolvable;这个 case 是 too-trivially-solvable。两端都要拦。
三、回答你的核心问题:哪些节点该上 LLM,哪些坚持用确定性方法
你的直觉对了——纯正则会在语义判断点失效,但不能全量上 LLM(DUA 要求本地模型 + 你要确定性可复现)。判断标准是:“结构/边界”问题用确定性方法,“语义/角色”问题用本地 LLM 打分,且 LLM 只输出结构化标签不改原文。 逐节点给方案:
关键设计原则:LLM 只做"打分/打标签",绝不改写或生成原文(temp=0,输出受约束 JSON)。这样既绕过正则失效,又不破坏确定性可复现,也符合 DUA(全程本地模型)。
具体我建议新增两个本地 LLM 质量闸门,产出写进 provenance:
1. Source attribution scorer:对 HPI 每个句子输出 {sentence_id, source∈{patient,collateral,chart_review,clinician_observed,osh_records}, confidence}。低 confidence 的留人工。G_hist 只收 source=patient。
2. Case admission scorer:对每个 case 输出 {diagnostic_reasoning_score: 0-5, cc_dx_distance: 0-5, gold_consistency: 0-5, solvability: bool, reasons}。diagnostic_reasoning_score < 阈值(像本 case)或 solvability=false(像胆囊炎 case HIDA 缺失)的直接踢出或转人工。
这两个闸门正好把你三个 pilot case 的问题全覆盖:Lewy body 的 collateral 污染、胆囊炎的 D_crit 缺失、本 case 的 trivial 诊断。

## 处理流水线总览
 原始 MIMIC 抽取(SQL/DuckDB)
        │
 ┌──────┴───────────────────────────────────────────────┐
 │ Stage A  确定性(deterministic)                       │
 │  A1 段落切分(正则锚点)                                │
 │  A2 de-id 回填(结构表 ↔ 文本 数值对齐)   ← Bug 1     │
 │  A3 lab 白名单过滤(itemid)                            │
 │  A4 出院查体/出院化验剔除(锚点切分)      ← Bug 4     │
 │  A5 lab_anchor_time 选择并落 provenance     ← Bug 2    │
 └──────┬───────────────────────────────────────────────┘
        │
 ┌──────┴───────────────────────────────────────────────┐
 │ Stage B  本地 LLM 闸门(只打分/打标签,不改写原文)     │
 │  B1 Source Attribution Scorer(HPI 句子角色)           │
 │  B2 Case Admission Scorer(可推理性/可解性/gold 一致)  │
 └──────┬───────────────────────────────────────────────┘
         │
 ┌──────┴───────────────────────────────────────────────┐
 │ Stage C  节点编译(scispaCy+negspaCy 主 + LLM 校验)    │
 │  C1 症状/发现 → CUI + OLDCARTS 属性节点                 │
 │  C2 平面归位 G_hist / G_exam / G_dx                     │
 │  C3 D / D_crit / candidates                             │
 └──────┬───────────────────────────────────────────────┘
        │
 ┌──────┴───────────────────────────────────────────────┐
 │ Stage D  QC gate(自动 + 人工)                         │
 │  D1 可解性校验(每个 D_crit 是否有可恢复来源)          │
 │  D2 admission 阈值过滤(trivial / unsolvable 踢出)     │
 │  D3 gold 子集人工 IAA                                   │
 └───────────────────────────────────────────────────────┘

 3. 方法选择对照表(哪步用什么,一页速查)

步骤方法上 LLM?备注段落切分正则锚点✗标题固定de-id 回填数值对齐✗Bug 1;查表非语义lab 垃圾行过滤itemid 白名单✗有限集合出院查体/化验剔除锚点切分✗补锚点即可lab_anchor_time规则 + provenance✗Bug 2HPI source 归属本地 LLM 打分✓ B1语义角色,正则失效诊断可推理性准入本地 LLM 打分✓ B2需医学语义症状→CUI+属性scispaCy/negspaCy 主 + LLM 校验半沿用现有两层D_crit 认定LLM 候选 + 人工终审半决定指标分母,留人工可解性校验集合判定✗D1,与 B2 联用