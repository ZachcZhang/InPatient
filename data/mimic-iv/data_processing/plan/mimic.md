# InPatient 实验数据集:从 MIMIC-IV 3.1 抽取与构建方案

> 目标:从 MIMIC-IV 重建一个**时间分层、节点级、可门控**的 gold patient graph,作为 Doctor / Patient / Exam 三 agent 问诊流程的底料;并据此构造测量 GVI(God-View Inflation)所需的实验条件。

---

## 0. 三个核心设计判断(先看这个)

**判断 1:MIMIC 没有对话,只有底料。**
"问诊记录"不是抽取对象,而是你的 agent 在 patient graph 上跑出来的产物。数据工程的真正目标 = 把出院小结(自由文本)+ 结构化表,重建成一个**节点级 gold graph G**,Patient agent 在其上做 DW/HL/AR/MR 门控,Exam agent 在其上返回检查结果,Evaluator 用它算 Coverage / Efficiency / Early Closure / Overconfidence / SafeScore。

**判断 2:时间分层是第一红线,决定数据是否泄漏。**
出院小结叙述的是**整段住院过程**。HPI/既往史是入院时患者已知的;Pertinent Results / Brief Hospital Course 是住院期间逐步揭晓的;Discharge Diagnosis 是答案。若不切分,Patient agent 会"未卜先知"。三层切分如下,且**恰好对应你的三平面**:

```
                          出院小结 + ED triage + labs/radiology
                                        │
                 ┌──────────────────────┼──────────────────────┐
                 ▼                      ▼                      ▼
        【入院时·患者可知】      【客观·检查可得】          【目标·隐藏】
            G_hist                  G_exam                   G_dx
   Chief Complaint / HPI /    Physical Exam(入院)/      Discharge Diagnosis /
   PMH / 社会史 / 家族史 /     Pertinent Results /          primary ICD(seq_num=1)
   过敏 / 入院用药 / ROS       labevents / radiology /     (Brief Hospital Course
                              microbio / triage vitals      仅用于构造,绝不暴露)
                 │                      │                      │
                 ▼                      ▼                      ▼
          Patient Agent            Exam Agent           Evaluator(隐藏)
        (DW/HL/AR/MR 门控)      (按 action 返回)        (算指标/MRR)

   D(available ceiling)= G_hist ∪ G_exam        ← Coverage 分母用 D,不用 G
   D_crit ⊂ D(诊断关键节点)                     ← Early Closure 参照
   G(full graph)= G_hist ∪ G_exam ∪ G_dx
```

**判断 3:节点要做到属性级粒度,门控才有意义。**
单个症状不是一个布尔节点,而是一个带 OLDCARTS/OPQRST 属性的对象(onset/quality/severity/location/radiation/...)。这样 Gatekeeper 才能在**属性级**门控——低 DW 时透露"胸痛"但藏住"放射到左臂";低 HL 时说得出"难受"但说不清"压榨样"。这是 Coverage/Efficiency 能算出有意义数值的前提,也是和 MedDialBench(整体行为脚本)/MAQuE(打包人格)做架构区分的发力点。

---

## 1. 数据源与模块清单(注意:要下载三个独立 PhysioNet 模块)

| 模块 | 版本 | 用途 | 关键表 |
|---|---|---|---|
| **MIMIC-IV** (hosp + icu) | v3.1 | 结构化:诊断、化验、用药、人口学 | `hosp/patients`, `hosp/admissions`, `hosp/diagnoses_icd`, `hosp/d_icd_diagnoses`, `hosp/labevents`, `hosp/d_labitems`, `hosp/microbiologyevents`, `hosp/omr`, `hosp/prescriptions` |
| **MIMIC-IV-Note** | v2.2 | **自由文本病历**(整个叙事骨架) | `note/discharge`, `note/radiology` |
| **MIMIC-IV-ED** | v2.2 | **主诉 + 初始生命体征 + 分诊**(presentation 的来源) | `ed/edstays`, `ed/triage`, `ed/vitalsign` |

> Note 和 ED 是和核心库**分开发布**的,很多人只下了 v3.1 核心库会发现没有病历文本。三个都要。
> ICU 模块(chartevents 等)v1 先不用——太细、且属于入院后监护,对"急诊presentation→诊断"的对话不必要,保持精简,后续要做严重度分层再加。

**引擎建议:用 DuckDB 直接读 `.csv.gz`,不要起 Postgres。** `labevents` 上亿行,先按 cohort 的 subject_id/hadm_id 下推过滤再 join,内存占用很低,适合 HPC 上单节点跑。

---

## 2. 队列定义与筛选

**纳入逻辑:成年、经 ED 入院、有出院小结、诊断本身需要推理。** 下面是 DuckDB 的 cohort CTE 骨架(路径按你的挂载改):

```sql
-- 建视图(DuckDB 可直接 glob 读 gz)
CREATE VIEW pat   AS SELECT * FROM read_csv_auto('hosp/patients.csv.gz');
CREATE VIEW adm   AS SELECT * FROM read_csv_auto('hosp/admissions.csv.gz');
CREATE VIEW dx    AS SELECT * FROM read_csv_auto('hosp/diagnoses_icd.csv.gz');
CREATE VIEW ddx   AS SELECT * FROM read_csv_auto('hosp/d_icd_diagnoses.csv.gz');
CREATE VIEW disch AS SELECT * FROM read_csv_auto('note/discharge.csv.gz');
CREATE VIEW eds   AS SELECT * FROM read_csv_auto('ed/edstays.csv.gz');
CREATE VIEW tri   AS SELECT * FROM read_csv_auto('ed/triage.csv.gz');

WITH primary_dx AS (         -- 主诊断(seq_num=1)+ 可读标题
  SELECT d.subject_id, d.hadm_id, d.icd_code, d.icd_version, t.long_title
  FROM dx d JOIN ddx t USING (icd_code, icd_version)
  WHERE d.seq_num = 1
),
cohort AS (
  SELECT a.subject_id, a.hadm_id, e.stay_id,
         p.anchor_age, p.gender, a.race, a.language,
         pd.icd_code, pd.icd_version, pd.long_title,
         ds.note_id, length(ds.text) AS note_len
  FROM adm a
  JOIN pat p        USING (subject_id)
  JOIN eds e        USING (subject_id, hadm_id)          -- 经 ED 入院
  JOIN primary_dx pd USING (subject_id, hadm_id)
  JOIN disch ds     USING (subject_id, hadm_id)          -- 有出院小结
  WHERE p.anchor_age >= 18
    AND a.hospital_expire_flag = 0                       -- v1 先排除院内死亡(可选)
)
SELECT * FROM cohort
WHERE note_len > 1500                                    -- 叙事足够丰富
  -- 排除"无诊断推理"的诊断:外伤(S/T)、产科(O)、Z码常规
  AND NOT regexp_matches(icd_code, '^(S|T|O|Z)')         -- ICD-10
;
```

**进一步筛选(在 Python 侧做,因为要看文本):**
- HPI 段落长度下限(如 < 400 字符剔除):没有足够病史就没法门控。
- HPI 中 `___`(去标识占位符)密度过高 → 标记低质量。
- **v1 建议按主诉家族限定范围**,让 Merck schema 的人工编写可控。先选 6–8 个经典急诊诊断性主诉:胸痛、腹痛、呼吸困难、晕厥、发热/脓毒、消化道出血、头痛、意识改变。每个主诉家族取若干高质量病例,既保证 Merck/UMLS grounding 覆盖,又能在因子设计里做主诉内对比。

---

## 3. 抽取与时间分层(核心步骤)

### 3.1 出院小结分段
MIMIC-IV-Note 的出院小结段落标题相当规整。用**锚定行首的大小写不敏感正则**做一级切分,LLM(temp=0,**本地模型**)做兜底/校验——这一步直接复用你已有的"确定性抽取 → 约束 LLM verifier"两层做法:

常见段标题(切分锚点):`Chief Complaint:`, `History of Present Illness:`, `Past Medical History:`, `Social History:`, `Family History:`, `Allergies:`, `Physical Exam:`, `Pertinent Results:`, `Brief Hospital Course:`, `Medications on Admission:`, `Discharge Diagnosis:`, `Discharge Condition:`。

### 3.2 按平面归位(时间分层的执行)

| 平面 | 取自 | 说明 |
|---|---|---|
| **G_hist**(Patient) | Chief Complaint, HPI, PMH, Social/Family History, Allergies, Medications on Admission | 入院时患者已知。HPI 是症状属性的主矿。 |
| **G_exam**(Exam) | Physical Exam(取**入院 admission exam**,不取 discharge exam), Pertinent Results, `labevents`, `radiology`, `microbiologyevents`, `ed/triage` vitals, `omr` | 医生下 action 才可得。 |
| **G_dx**(隐藏) | Discharge Diagnosis 文本 + `diagnoses_icd` seq_num=1 + 用 Brief Hospital Course **仅做构造** | 对两个 agent 都不可见;Brief Hospital Course 绝不进 G_hist/G_exam。 |

> **Physical Exam 段常含 admission 和 discharge 两套**,只取 admission 那套进 G_exam,否则泄漏治疗后状态。

### 3.3 结构化表与文本的对齐
`labevents` 的化验值是结构化 ground truth,Pertinent Results 是其文本摘要;**以结构化表为准**,文本仅用于补 normal range 解读和影像/micro 的定性结论。按 `hadm_id` + `charttime` 过滤到本次住院、且时间上"入院后早期"的化验(给 Exam agent 当作"医生下单后能拿到的结果")。

---

## 4. 黄金患者图 schema

### 4.1 症状/发现节点(属性级)
```json
{
  "node_id": "sym_001",
  "type": "symptom",                 // symptom | history | exam | lab | imaging | micro
  "plane": "patient",                // patient | exam | target
  "cui": "C0008031",                 // UMLS,用 scispaCy UMLS linker
  "name": "chest pain",
  "polarity": "present",             // negspaCy: present | absent
  "attributes": {                    // OLDCARTS;缺省为 null
    "onset": "3 days ago", "duration": null, "quality": "pressure-like",
    "severity": "7/10", "location": "substernal", "radiation": "left arm",
    "timing": "exertional", "context": "...", "modifying_factors": "relieved by rest",
    "associated": ["dyspnea", "diaphoresis"]
  },
  "salience": "critical",            // critical | supporting | incidental → 用于 D_crit
  "source": {"section": "HPI", "char_span": [1203, 1457]},
  "provenance": "deterministic|verifier"
}
```

### 4.2 案例冻结产物(frozen JSON,沿用你现有的 artifact 格式)
```json
{
  "case_id": "inp_000123",
  "source": {"subject_id": 0, "hadm_id": 0, "stay_id": 0},
  "presentation": {                  // Doctor agent 初始唯一可见
    "age": 58, "sex": "M",
    "chief_complaint": "chest pain",
    "triage_vitals": {"hr": 98, "bp": "150/92", "rr": 18, "spo2": 96, "temp": 37.0},
    "acuity": 2
  },
  "G_hist":  [ /* patient-plane 节点 */ ],
  "G_exam":  [ /* exam-plane 节点 */ ],
  "G_dx": {
    "primary_icd": "I21.4", "title": "NSTEMI",
    "discharge_dx_text": "...",
    "candidates": ["I21.4","I20.0","K21.9","I26.99","..."]   // gold + distractor,供 MRR
  },
  "D":      ["sym_001","exam_004","lab_010", "..."],          // = G_hist ∪ G_exam 的 id
  "D_crit": ["sym_001","lab_010","exam_004"],                 // 关键子集(见 4.3)
  "disease_schema": {                                          // 来自 Merck
    "diagnosis_cui": "C0151744",
    "key_criteria": [ {"cui":"C0008031","name":"chest pain","weight":1.0}, "..." ]
  },
  "qc": {"iaa": 0.81, "note_deid_density": 0.03, "verifier_pass": true}
}
```

### 4.3 D、D_crit、候选集怎么来
- **D** = G_hist ∪ G_exam 的全部 node_id(你已确定 Coverage 分母用 D 而非 G,以隔离医生技能)。
- **D_crit**(诊断关键节点)= **Merck schema ∩ 本病例实例**,再人工校验。做法:用 UMLS CUI 把 Merck Manual 中该诊断的"典型表现/诊断标准"normalize 成概念集,与本病例 D 里的节点求交;交集即"这个病人身上确实存在、且与诊断相关"的关键节点。这正是 UMLS + Merck 两个知识源的结合点——UMLS 做跨源概念对齐,Merck 提供疾病层 schema。
- **candidates(MRR 候选集)**:gold + 干扰项。干扰来自 ①该主诉的 Merck 鉴别诊断列表,②同 ICD 父类的 sibling 码,③数据中与该主诉高频共现的诊断。GVI 用 MRR 衡量,所以**每个案例必须冻结一个候选排序集**,否则算不出 full vs constrained 的 MRR gap。

---

## 5. 三个 Agent 的数据供给

| Agent | 初始可见 | 运行时可取 | 不可见 |
|---|---|---|---|
| **Doctor** | `presentation`(年龄/性别/主诉/分诊体征/acuity) | 通过提问从 Patient 获取 G_hist 子集;通过下检查 action 从 Exam 获取 G_exam 子集 | G_dx,以及未被问到/未被检查的节点 |
| **Patient** | G_hist 全量 + 行为参数(DW/HL/AR/MR) | —(只应答) | G_exam, G_dx |
| **Exam** | G_exam 全量(+ 每项的可得性/延迟标签) | —(按 Doctor 的 order 返回对应节点) | G_hist, G_dx |

**Patient agent 的门控级联**沿用你的 `MR → DW/AR → HL/Verbalizer`:
- **MR(Memory Retention)** 最先作用:决定该节点/属性是否"还记得"——记不得的,后续门一律取不到(这是你区别于所有竞品的独立轴,MedDialBench/MAQuE/PatientSim 都没单独建模遗忘)。
- **DW / AR** 决定"愿不愿意/情绪是否允许"透露该节点或某属性。
- **HL / Verbalizer** 决定"能不能用准确的词说出来"——HL 低时把 clinical concept 降级为 lay 表达(quality 属性最易丢失)。
- Grounding Agent(BM25⊕FAISS)在这里的作用:把 Doctor 的自然语言提问匹配到 G_hist 中的目标节点,再交给 Gatekeeper 判定能否释放。

**Exam agent** 的设计要点:不是一问就全给。给每个 G_exam 节点打 `modality`(physical / lab / imaging / micro)和可得性,Doctor 必须下对应 action(查体 / 开化验 / 开影像)才返回对应子集——这样 Efficiency 才能区分"会查体的医生"和"乱开检查的医生"。

---

## 6. 完整问诊流程的数据流与实验条件(用于测 GVI)

### 6.1 单次问诊 episode 的数据流
```
init: Doctor ← presentation;  Patient ← G_hist + (DW,HL,AR,MR);  Exam ← G_exam
loop (t = 1..T):
   Doctor 产出 action ∈ {Ask(q), Order(exam), Diagnose(ranked_list), Abstain}
     ├ Ask(q):    Grounding 把 q 映射到 G_hist 节点 → Gatekeeper(MR→DW/AR→HL)
     │            → Verbalizer 渲染 → Doctor 更新 belief,记录"已恢复节点"
     ├ Order(e):  Exam 返回 e 对应 G_exam 节点 → Doctor 更新 belief
     └ Diagnose / Abstain → 结束
eval: 对照 D_crit 计算 Coverage / Efficiency / Early Closure / Overconfidence / SafeScore
      对照 candidates 计算诊断 MRR
```

### 6.2 两个对照条件(GVI 的定义直接挂这里)
- **Full-Information(God-View)条件**:把 D 全量一次性塞给 Doctor,直接出诊断 → 得到 MRR_full(被"虚增"的天花板)。
- **Constrained-Dialogue 条件**:Doctor 必须经由受约束的 Patient + Exam 把 D 一点点挖出来 → 得到 MRR_constrained。
- **GVI = MRR_full − MRR_constrained**(按你已定的 MRR-gap 定义)。
- **剂量-反应**:在 (DW,HL,AR,MR) 网格上扫描,画出每个轴单独变化时 MRR/Coverage 的下降曲线。论文里要展示**符号化门控产生单调、可审计的剂量-反应曲线**,直接对位 MedDialBench 的 dose-response 结果——这是你架构主张的实证落点。

---

## 7. 质量控制(三层,复用你已有的流程)

1. **确定性抽取**:scispaCy(`en_core_sci_lg` + UMLS linker)+ negspaCy 抽节点与极性 → NetworkX DiGraph。
2. **约束 LLM verifier(temp=0,本地模型)**:校验节点是否忠实于原文、属性归位是否正确、平面归属是否泄漏(尤其抓"Brief Hospital Course 的内容被误放进 G_hist/G_exam")。
3. **人工 + IAA**:在 gold 子集(目标 ~150–300 例)上做双标注,报 Cohen's κ / Gwet's AC₁。D_crit 的判定**必须过人工**,因为它直接决定所有指标的分母。

> 规模策略:自动抽取一个大池(数千例)→ 自动 QC 过滤 → 人工精校出 gold 子集(~200 例)。参考量级:PatientSim 170、MedDialBench 85、AIPatient 1495、DiagnosisArena 1113。AAAI 投稿 ~200 高质量例足够,且人工可负担。

---

## 8. 合规红线(务必)

PhysioNet 的 credentialed DUA **禁止把数据交给第三方**。把 MIMIC-IV-Note 的病历文本发给外部 LLM API(OpenAI/Anthropic 公网端点等)= 向第三方泄露,属违约。**所有涉及 note 文本的解析/抽取/verifier 步骤一律走你 HPC 上的本地模型**(DeepSeek-V3 等),这恰好也是你集群已经部署好的能力。外部 API 仅可用于**不含 MIMIC 文本**的辅助任务(如 Merck schema 的通用整理)。注意 Brief Hospital Course / Discharge Diagnosis 即便在内部也要严格限制流向,避免污染 patient/exam 平面。

---

## 9. 落地顺序(精简优先,先打通一条主诉)

1. 下齐三模块;DuckDB 建视图;跑 §2 cohort,先**只取"胸痛"主诉**,得到 N≈百例的小池。
2. 写出院小结分段器(正则 + 本地 LLM 兜底),输出按平面归位的 raw sections。
3. 把你现有 scispaCy+negspaCy→NetworkX 管线接上,产出属性级节点,冻结成 §4.2 的 JSON。
4. 人工写"胸痛家族"的 Merck schema(NSTEMI/STEMI/不稳定心绞痛/PE/主动脉夹层/GERD/...),算 D_crit + candidates。
5. 在 5–10 个案例上跑通完整 episode(Doctor/Patient/Exam),验证指标能算出来、平面无泄漏。
6. 确认无误后再横向扩到其余主诉家族。

---

## 10. 风险与开放问题

- **HPI 是临床语言,不是患者语言**:节点存 clinical concept,患者口吻由 Verbalizer + HL 降级生成;不要把 HPI 原句当患者话术。
- **ICD 是计费驱动**:primary ICD 偶尔不是"诊断谜题"的答案。用 Discharge Diagnosis 文本 + primary ICD 双向对齐,冲突时人工裁定。
- **去标识占位符 `___`**:涉及关键属性(如具体时长/部位)被打码的节点,要么降级(标 unknown),要么剔除该案例,别让 verbalizer 编造。
- **化验时间界面**:哪些化验算"医生下单后可得"需要一个时间阈值(如入院后 X 小时内),这条规则会影响 G_exam 的边界,建议显式记录在 case 的 provenance 里以便复现。
- **candidates 干扰项的难度**:太易则 MRR 触顶、GVI 不显著;太难则 full 条件也崩。建议干扰项难度也作为一个可报告的设计变量。