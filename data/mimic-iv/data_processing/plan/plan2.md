构建一个基于 MIMIC-IV 的 InPatient（住院患者）多 Agent 模拟实验，核心挑战在于：**MIMIC-IV 是静态的回顾性电子病历（EHR）数据，而你需要的是动态的交互环境。** 

因此，我们的核心思路是：**将 MIMIC-IV 的静态数据转化为 Agent 的“初始剧本（Initial State）”、“环境状态机（Environment State）”和“评估基准（Ground Truth）”。**

以下是完整的数据抽取与构建方案：

---

### 一、 数据抽取方案 (Data Extraction Strategy)

我们需要从 MIMIC-IV v3.1 中抽取信息完整、具有代表性的住院病例。

#### 1. 目标队列筛选 (Cohort Selection)
为了保证实验质量，不能抽取所有数据。建议筛选满足以下条件的 `hadm_id` (住院号)：
*   **成人患者**：年龄 $\ge$ 18 岁。
*   **住院时长**：住院时间 $> 24$ 小时（排除门诊或极短观察）。
*   **数据完整性**：至少有 1 条 Lab 检查记录，至少有 1 个出院诊断（ICD 编码），且包含出院小结（Discharge Note，用于提取患者病史）。

#### 2. 核心 SQL 抽取逻辑 (PostgreSQL)
以下 SQL 用于构建基础宽表，将患者信息、诊断、检查整合在一起。

```sql
-- 1. 筛选目标队列 (Cohort)
WITH target_admissions AS (
    SELECT 
        a.subject_id, 
        a.hadm_id, 
        p.anchor_age + a.admittime::date - p.anchor_age::date AS age, -- 计算入院年龄
        p.gender,
        a.admission_type,
        a.admittime,
        a.dischtime,
        a.hospital_expire_flag,
        a.race,
        a.marital_status,
        a.language
    FROM mimiciv_hosp.admissions a
    JOIN mimiciv_hosp.patients p ON a.subject_id = p.subject_id
    WHERE EXTRACT(YEAR FROM a.admittime) - p.anchor_year + p.anchor_age >= 18
      AND EXTRACT(HOUR FROM a.dischtime - a.admittime) >= 24
),

-- 2. 提取诊断结果 (Ground Truth)
diagnoses AS (
    SELECT 
        d.hadm_id,
        di.icd_code,
        di.long_title,
        d.seq_num -- 诊断排序，seq_num=1 通常是主要诊断
    FROM mimiciv_hosp.diagnoses_icd d
    JOIN mimiciv_hosp.d_icd_diagnoses di ON d.icd_code = di.icd_code AND d.icd_version = di.icd_version
    WHERE d.hadm_id IN (SELECT hadm_id FROM target_admissions)
),

-- 3. 提取实验室检查 (Exam Data)
labs AS (
    SELECT 
        l.hadm_id,
        l.charttime,
        di.itemid,
        di.label AS lab_name,
        l.valuenum,
        l.valueuom,
        l.flag -- 异常标志 (abnormal/normal)
    FROM mimiciv_hosp.labevents l
    JOIN mimiciv_hosp.d_labitems di ON l.itemid = di.itemid
    WHERE l.hadm_id IN (SELECT hadm_id FROM target_admissions)
      AND l.valuenum IS NOT NULL
),

-- 4. 提取影像检查报告 (Radiology)
radiology AS (
    SELECT 
        r.hadm_id,
        r.charttime,
        r.note_type, -- 如 CT, MRI, X-ray
        r.text AS report_text
    FROM mimiciv_hosp.radiology r -- 注：若文本在 note 模块，请改为 mimiciv_note.radiology
    WHERE r.hadm_id IN (SELECT hadm_id FROM target_admissions)
)

-- 5. 最终输出 (实际应用中建议导出为 CSV/Parquet)
SELECT * FROM target_admissions;
-- 后续在 Python 中将 diagnoses, labs, radiology 按 hadm_id 聚合为 JSON 数组关联。
```

#### 3. 非结构化文本提取 (NLP/LLM 辅助)
MIMIC 的 `discharge` (出院小结) 包含了最真实的患者主诉和病史。你需要使用 LLM（如 GPT-4/Qwen）对 `discharge` 文本进行信息抽取，生成结构化的 JSON：
*   **Chief Complaint (主诉)**：患者入院的最主要原因。
*   **HPI (现病史)**：症状的发生、发展、伴随症状。
*   **PMH (既往史)**：过去的疾病、手术史。
*   **Social History (社会史)**：吸烟、饮酒、职业等。

---

### 二、 数据后续使用方案 (Agent 建模与交互设计)

将抽取的数据转化为三个 Agent 的“大脑”和“环境”。

#### 1. Patient Agent (患者 Agent)
*   **角色定位**：模拟真实患者，通常缺乏医学专业知识，可能焦虑，且**不会主动全盘托出所有信息**。
*   **输入 (System Prompt & State)**：
    *   **人设剧本**：注入上述 NLP 提取的 `Chief Complaint`, `HPI`, `PMH` 等。
    *   **隐藏状态**：将一些次要症状或敏感信息（如性病、吸毒史）标记为“隐藏”，只有当医生 Agent 明确询问相关隐私或特定症状时才释放。
*   **Action 空间**：
    *   `reply(text)`: 回答医生的问题。
    *   `ask_question(text)`: 向医生提问（如“我严重吗？”）。
    *   `show_emotion(emotion)`: 表达情绪（如疼痛、焦虑）。
*   **交互逻辑**：使用 LLM 驱动。Prompt 需严格限制：“*你是一个患者，你不知道自己的确切诊断。你只能根据医生问的问题，从你的病历中回忆并回答。如果医生没问，不要主动说出所有症状。使用非专业的大白话。*”

#### 2. Exam Agent (检查 Agent / 环境模拟器)
*   **角色定位**：模拟医院的 LIS（检验系统）和 PACS（影像系统），负责响应医生的开单请求并返回结果。
*   **输入**：医生 Agent 发出的 Action（如 `order_test("血常规")`）。
*   **核心逻辑 (State Machine)**：
    1.  **意图识别与映射**：将医生的自然语言请求映射到 MIMIC 的 `itemid`。例如，医生输入“查一下肝功”，Exam Agent 通过内置字典或向量检索，映射到 MIMIC 中的 `ALT`, `AST`, `Total Bilirubin` 等 `itemid`。
    2.  **查表返回**：在预先抽取的 `labs` 或 `radiology` 数据中，查找该 `hadm_id` 对应时间点的真实数据。
    3.  **结果格式化**：将数值结果格式化，并**高亮异常值**（利用 MIMIC 自带的 `flag` 字段，或对比正常参考范围）。
*   **Action 空间**：
    *   `return_result(test_name, value, unit, is_abnormal, reference_range)`
    *   `return_report(imaging_type, text_summary)` (对于影像，可返回原始报告或让 LLM 生成一个简短的 Findings)。

#### 3. Doctor Agent (医生 Agent)
*   **角色定位**：模拟住院医/主治医，目标是收集信息、做出诊断、制定计划。
*   **输入**：Patient Agent 的回答，Exam Agent 的检查结果。
*   **Action 空间 (Tool Calling / Function Calling)**：
    *   `ask_patient(question)`: 向患者提问。
    *   `order_exam(exam_name)`: 开具检查（调用 Exam Agent）。
    *   `make_diagnosis(icd_code_or_disease_name, confidence)`: 给出初步或最终诊断。
    *   `order_treatment(medication_or_procedure)`: 下达医嘱。
    *   `end_consultation()`: 结束问诊。
*   **交互逻辑**：遵循标准的临床思维（SOAP：Subjective, Objective, Assessment, Plan）。系统需监控其 Action 序列，确保其逻辑合理。

---

### 三、 实验数据集最终结构 (JSON Schema)

为了支持上述多 Agent 框架（如 AutoGen, LangGraph），你需要将每个病例封装成一个标准的 JSON 对象。以下是单个 Case 的数据集结构：

```json
{
  "case_id": "hadm_2349857",
  "metadata": {
    "age": 65,
    "gender": "M",
    "admission_type": "EMERGENCY",
    "ground_truth_diagnoses": ["I21.9 (Acute myocardial infarction)", "E11.9 (Type 2 diabetes)"],
    "ground_truth_procedures": ["PCI (Percutaneous coronary intervention)"]
  },
  
  "patient_agent_config": {
    "persona": "65岁男性，退休工人，性格有些固执，对疼痛比较敏感。",
    "chief_complaint": "胸痛伴大汗 3 小时。",
    "hpi": "3小时前搬重物时突发胸骨后压榨性疼痛，向左肩放射，休息后不缓解，伴大汗淋漓、恶心。",
    "pmh": ["高血压 10 年，平素服药不规律", "2型糖尿病 5 年"],
    "social_history": "吸烟 40 年，每天 1 包。偶尔饮酒。",
    "hidden_symptoms": ["近期有黑矇现象（未主动告知）"]
  },

  "exam_agent_config": {
    "available_labs": {
      "Troponin T": {"value": 1.5, "unit": "ng/mL", "ref_range": "<0.01", "flag": "HIGH", "charttime": "2120-05-15 14:30:00"},
      "Glucose": {"value": 180, "unit": "mg/dL", "ref_range": "70-100", "flag": "HIGH", "charttime": "2120-05-15 14:30:00"}
    },
    "available_radiology": {
      "CXR": {"findings": "Cardiomegaly, no acute infiltrates.", "charttime": "2120-05-15 15:00:00"}
    },
    "test_mapping_dict": {
      "肌钙蛋白": "Troponin T",
      "血糖": "Glucose",
      "胸片": "CXR"
    }
  },

  "evaluation_metrics": {
    "required_exams": ["Troponin T", "ECG", "CXR"],
    "max_allowed_turns": 20,
    "critical_actions": ["order_exam(ECG)"] 
  }
}
```

---

### 四、 实验流程与评估方案

在构建好数据集后，你的实验流程如下：

1.  **初始化**：加载一个 JSON Case。将 `patient_agent_config` 注入 Patient LLM，将 `exam_agent_config` 注入 Exam 逻辑（或 LLM），将初始状态（如分诊台护士的简单记录）注入 Doctor LLM。
2.  **多轮交互**：Doctor Agent 和 Patient/Exam Agent 进行多轮对话。Doctor 必须通过 Function Calling 来执行 `ask_patient` 或 `order_exam`。
3.  **终止条件**：Doctor 调用 `make_diagnosis` 且置信度达标，或达到最大轮数。

#### 如何评估 Agent 的表现？
由于是 InPatient 场景，评估不能仅靠对话流畅度，必须基于**临床有效性**：

1.  **诊断准确率 (Diagnostic Accuracy)**：
    *   将 Doctor Agent 输出的诊断与 `metadata.ground_truth_diagnoses` 对比。
    *   计算主要诊断（First Diagnosis）的命中率（Exact Match 或 ICD 层级匹配）。
2.  **检查合理性 (Exam Efficiency & Recall)**：
    *   **Recall**：Doctor 是否开具了 `evaluation_metrics.required_exams` 中的关键检查（如胸痛患者必须开 ECG 和肌钙蛋白）。
    *   **Precision**：Doctor 是否开具了大量无关的“大包围”检查（可通过惩罚过度开单来评估）。
3.  **问诊完整度 (History Taking Completeness)**：
    *   通过 LLM-as-a-Judge，让另一个评估 LLM 审查对话记录，判断 Doctor 是否问出了 `patient_agent_config` 中的关键 HPI 和 PMH 信息。
4.  **安全性 (Safety)**：
    *   如果 Patient Agent 表现出危急值（如 `exam_agent` 返回肌钙蛋白极高），Doctor Agent 是否立即停止了常规问诊并下达了紧急干预医嘱（如 `order_treatment("Aspirin", "Heparin")` 或呼叫抢救）。

### 总结建议
这个方案的核心在于 **“将静态 EHR 转化为动态状态机”**。在实施时，建议先选取 MIMIC-IV 中 100-200 个典型且数据完整的病例（如急性心梗、肺炎、急性阑尾炎等常见急症）进行小规模 Pilot 实验，调通 Patient 的“隐瞒”逻辑和 Exam 的“映射”逻辑后，再扩大数据集规模。