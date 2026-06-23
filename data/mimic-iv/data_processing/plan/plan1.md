MIMIC-IV 切分方案
先讲一个前提：这是评估 benchmark，不是训练任务——doctor 是被评估的 frozen LLM，没有模型训练需求。所以核心只需 dev + frozen test，gate config 是 case 内的 within-factor（每个 case 在所有 config 下各跑一遍），切分只切 case（病人）身份。
Step 0 — 数据源 & 一病人一例

用 MIMIC-IV-Note 的 discharge summary。每个 subject_id 只取一个 hadm_id（取 summary 最丰富的那次，或固定随机 seed 取一次）。这一步同时解决两件事：subject 级 disjoint 自动成立（每个病人全局只出现一次），且消除同一病人多次入院的 intra-patient correlation。N 会小一点，但干净，符合你 lean+rigorous 的取向。
Step 1 — Eligibility filter（切分前）

primary diagnosis 可得（ICD-9/10 → 映射 CCSR category 作为 doctor 要命中的「答案」）；
抽图后 node 数 ∈ [N_min, N_max]，建议 [10, 40]：太短没有多轮披露动态，太长不可控；
age ≥ 18，age/gender 字段齐全（HL/DW 的人群先验要用）；
院内死亡病例可保留（诊断 ground truth 有效），但剔除 summary 结构退化的；
去模板化/去重复段落。

Step 2 — Stratify

按 (CCSR body-system × case complexity 分箱) 双向分层，并打 rare flag（诊断类别频率低于某 percentile）。务必保证 test 内 rare 子集量够做你的 rare-disease 子分析（对标 KnowGuard 的 rare-case 优势论证）。分层后在每层内做 subject-disjoint 随机划分。
Step 3 — 三个集合
集合比例用途Dev / Calibration15%调 gate 阈值、retrieval 超参、verifier/verbalizer prompt——所有你「会看」的迭代都在这；mock pipeline 反复跑也在这Test (frozen)85%正式 benchmark，开发期绝不查看Platinum（test 的子集）~150 例人工 gold-graph 标注，做 inter-annotator agreement + 抽图质量估计，并作为可信度锚点：主结果报 full auto-extracted test，同时报 platinum 上的结果，证明 metric 在高质量子集上稳健（类比 MMLU 的 clean subset）
几个 MIMIC 专属的坑要标注：

Contamination:大模型可能见过 MIMIC 衍生文本。discharge summary 受 PhysioNet credentialed 保护、泄漏概率低于 MIMIC-III，但非零。好在你的 constrained-disclosure setup 是天然缓解——答案不可被 doctor 直接检索，必须主动 elicit，削弱了 memorization 的作用。论文里点一句即可。
时间切分(可选):MIMIC-IV 有 anchor_year_group，如想测分布漂移鲁棒性可改成时序切（早期训/晚期测）；评估-only benchmark 里非必需，subject-disjoint 才是 must-have。
去标识 age:≥89 岁被聚合，处理人群先验时注意这个上限。