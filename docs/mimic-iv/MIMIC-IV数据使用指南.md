# MIMIC-IV 数据使用指南（学习专用）

> 面向第一次上手 MIMIC-IV 的研究者。读完你应能：看懂三大模块与表关系、理解去标识机制、用 DuckDB 写出常见查询、避开新手坑。
>
> 本指南基于本机已下载的版本：**MIMIC-IV v3.1（hosp）+ MIMIC-IV-ED v2.2 + MIMIC-IV-Note v2.2**。

---

## 1. MIMIC-IV 是什么

MIMIC-IV 是 MIT 实验室与 BIDMC 医院合作发布的**大型去标识化重症/住院电子病历数据库**，覆盖约 30 万名患者、40 万+ 次住院。它是 credentialed 数据：需在 PhysioNet 完成 CITI 培训并签署 DUA 才能下载。

数据以 **`.csv.gz`** 形式发布，按"模块"拆分下载。**核心 hosp 库与 Note、ED 是分开发布的**——只下了 v3.1 核心库会发现没有病历文本，三个都要下。

---

## 2. 四大模块与磁盘布局

```
physionet.org/files/mimiciv/
├── mimic-iv/                 # 核心库 (v3.1)
│   ├── hosp/                 # 住院期间的医院信息系统数据(诊断/化验/用药/人口学)
│   └── icu/                  # ICU 监护数据(chartevents 等, 极细)
├── mimic-iv-ed/2.2/ed/       # 急诊科(主诉/分诊/生命体征)
└── mimic-iv-note/2.2/note/   # 自由文本病历(出院小结/影像报告)
```

| 模块 | 内容 | 何时用 |
|---|---|---|
| **hosp** | 结构化：诊断、化验、用药、人口学、微生物 | 几乎所有研究的主干 |
| **icu** | 床旁监护逐分钟数据（chartevents 上亿行） | 只在做 ICU 时序/严重度时用 |
| **ed** | 急诊主诉 + 初始生命体征 + 分诊 | 需要"presentation/就诊起点"时 |
| **note** | 出院小结、影像报告（自由文本） | 需要叙事性病史/报告时 |

---

## 3. 三个核心 ID（最重要的一节）

MIMIC 用三层 ID 把所有表串起来，**搞懂它们就懂了一半**：

| ID | 粒度 | 含义 |
|---|---|---|
| `subject_id` | **病人** | 一个人，跨多次住院稳定 |
| `hadm_id` | **一次住院** | 一次 admission，从入院到出院 |
| `stay_id` | **一段停留** | ED 一次就诊 / 或 ICU 一次转入 |

关系：`1 个 subject_id → N 次 hadm_id → 每次住院可含 0~N 个 stay_id`。

```
subject_id (病人)
   └── hadm_id (住院 A)
   │      ├── stay_id (ED 就诊)        ← ed/edstays
   │      └── stay_id (ICU 停留)       ← icu/icustays
   └── hadm_id (住院 B)
```

> 关键坑：**有些表只有 `subject_id` 没有 `hadm_id`**（如 `omr` 门诊测量、部分 `labevents` 门诊化验、ED 的 `triage`/`vitalsign` 只有 `stay_id`）。连表前先确认主键。

---

## 4. 去标识化机制（不懂会算错）

为保护隐私，MIMIC 做了三类处理：

1. **日期平移**：每个病人的所有时间被随机平移到 **2100–2200 年**之间的未来。
   - 同一病人内部**时间间隔保持真实**（可算住院时长、化验间隔）；
   - 跨病人**绝对时间无意义**（不能比较两个病人谁先入院）。
2. **年龄锚点**：不直接给年龄，而给三个字段：
   - `anchor_age`：病人在 `anchor_year` 那年的年龄；
   - `anchor_year`：一个平移后的参考年；
   - `anchor_year_group`：真实年份所属区间（如 `2011-2013`），做时间漂移分析时用它。
   - **某次入院年龄** = `anchor_age + (year(admittime) - anchor_year)`。
   - ⚠️ 年龄 **≥ 89 岁会被统一聚合**（上限处理），做人群先验时注意。
3. **文本打码**：自由文本里的姓名/日期/地点等被替换成 **`___`**。处理 note 时要么把含 `___` 的关键属性降级为 unknown，要么剔除该样本，**切勿让模型脑补**。

---

## 5. 模块表速查（含真实列名）

### 5.1 hosp（核心）

| 表 | 主键 | 关键列 | 备注 |
|---|---|---|---|
| `patients` | subject_id | gender, anchor_age, anchor_year, anchor_year_group, dod | 人口学 + 死亡日期 |
| `admissions` | hadm_id | admittime, dischtime, deathtime, admission_type, race, marital_status, language, insurance, edregtime, edouttime, hospital_expire_flag | 一次住院的元信息 |
| `diagnoses_icd` | (hadm_id, seq_num) | icd_code, icd_version, **seq_num** | **seq_num=1 为主诊断** |
| `d_icd_diagnoses` | (icd_code, icd_version) | long_title | ICD 码 → 可读标题（字典表） |
| `labevents` | labevent_id | subject_id, hadm_id, itemid, charttime, value, valuenum, valueuom, ref_range_lower/upper, **flag**, priority | **2.5GB**，化验主表；`flag='abnormal'` 标异常 |
| `d_labitems` | itemid | label, fluid, category | 化验项字典（Blood/Urine…） |
| `microbiologyevents` | microevent_id | spec_type_desc, test_name, org_name, ab_name, interpretation, charttime | 培养/药敏；阴性结果 org_name 可能为空 |
| `omr` | (subject_id, chartdate) | result_name, result_value | 门诊测量（身高/体重/血压/BMI），**无 hadm_id** |
| `prescriptions` / `pharmacy` / `emar` | hadm_id | 药名、剂量、给药时间 | 用药链路，较细 |
| `procedures_icd` | hadm_id | icd_code, icd_version | 手术/操作（会泄漏诊断答案，慎用） |

### 5.2 ed（急诊）

| 表 | 主键 | 关键列 |
|---|---|---|
| `edstays` | stay_id | subject_id, hadm_id, intime, outtime, arrival_transport, disposition |
| `triage` | stay_id | **chiefcomplaint**, temperature, heartrate, resprate, o2sat, sbp, dbp, pain, **acuity** |
| `vitalsign` | (stay_id, charttime) | 急诊期间多次生命体征 |
| `diagnosis` | (stay_id, seq_num) | icd_code, icd_version, icd_title |

> `edstays.hadm_id` 不为空 ⇒ 该次急诊最终**收入住院**，这是把 ED 与 hosp 连起来的桥。

### 5.3 note（病历文本）

| 表 | 主键 | 关键列 | 大小 |
|---|---|---|---|
| `discharge` | note_id | subject_id, hadm_id, charttime, **text** | 1.1GB |
| `radiology` | note_id | subject_id, hadm_id, charttime, **text** | 780MB |
| `*_detail` | note_id | 字段级元数据 | 小 |

出院小结 `text` 的段落标题很规整（`Chief Complaint:` / `History of Present Illness:` / `Past Medical History:` / `Physical Exam:` / `Pertinent Results:` / `Brief Hospital Course:` / `Discharge Diagnosis:` …），可用锚定行首的正则切分。

---

## 6. ICD-9 与 ICD-10 并存

`diagnoses_icd` 里 `icd_version` 同时有 **9 和 10**，两套编码体系不通用：

- 连 `d_icd_diagnoses` 取标题时**必须同时 join `icd_code` 和 `icd_version`**，否则错配。
- 跨版本统计需先用 GEM 映射或 CCSR 归并到统一类别。
- ICD-10 外伤=`S/T`、产科=`O`、健康因素=`Z`；ICD-9 外伤/中毒≈800–999、外因=`E`、补充=`V`。

---

## 7. 用 DuckDB 查询（推荐姿势）

亿行级 CSV 不必起 Postgres。**DuckDB 直接读 `.csv.gz`**，内存占用低、单机够用。

```bash
pip install duckdb
```

```python
import duckdb
con = duckdb.connect()
HOSP = "/Volumes/Elements/数据/physionet.org/files/mimiciv/mimic-iv/hosp"

# 技巧1: MIMIC 脏数据多, 先全列按 VARCHAR 读, 再显式 TRY_CAST
def csv(p):
    return f"read_csv('{p}', all_varchar=true, header=true, compression='gzip', ignore_errors=true)"

# 技巧2: 主诊断 + 可读标题
con.sql(f"""
SELECT d.subject_id, d.hadm_id, d.icd_code, t.long_title
FROM {csv(HOSP+'/diagnoses_icd.csv.gz')} d
JOIN {csv(HOSP+'/d_icd_diagnoses.csv.gz')} t
  ON d.icd_code = t.icd_code AND d.icd_version = t.icd_version
WHERE d.seq_num = '1'
LIMIT 10
""").show()
```

**处理超大表（labevents 2.5GB）的关键技巧 —— 半连接下推**：先把目标队列做成小表，再让大表只保留命中行，单次扫描即可：

```python
con.execute(f"CREATE TABLE cohort AS SELECT DISTINCT CAST(hadm_id AS BIGINT) hadm_id FROM ... LIMIT 200")
con.sql(f"""
SELECT l.hadm_id, l.itemid, l.valuenum, l.flag
FROM {csv(HOSP+'/labevents.csv.gz')} l
SEMI JOIN cohort c ON CAST(l.hadm_id AS BIGINT) = c.hadm_id
""")
```

其他常用：`QUALIFY ROW_NUMBER() OVER (...)` 去重取每项首条；`TRY_CAST(x AS TIMESTAMP)` 安全转时间；用 `EXTRACT(EPOCH FROM (dischtime-admittime))/3600` 算住院小时。

---

## 8. 新手最常踩的 10 个坑

1. **绝对时间无意义**：日期被平移，只能比同一病人内部的相对时间。
2. **年龄要算**：`patients.anchor_age` 不是入院年龄，需用 anchor 公式换算；≥89 被截顶。
3. **ICD 双版本**：join 字典表务必带 `icd_version`。
4. **主键不一致**：`triage`/`vitalsign` 只有 `stay_id`，`omr` 只有 `subject_id`，连表前先确认。
5. **labevents 有门诊行**：`hadm_id` 可能为空（非住院化验），按住院分析要先过滤。
6. **`___` 占位符**：自由文本去标识，别让模型脑补被打码的关键信息。
7. **出院小结是"整段叙事"**：含入院前史 + 住院全程 + 出院结论，做"诊断前可见信息"时**必须按时间分层**，否则信息泄漏。
8. **Physical Exam 有两套**：常含 admission 与 discharge 两段，研究入院状态只取 admission。
9. **ICD 是计费驱动**：`seq_num=1` 偶尔不是真正"诊断谜题"的答案，必要时与 Discharge Diagnosis 文本互校。
10. **CSV 类型推断失败**：大表混入异常值会让 DuckDB 推断报错，用 `all_varchar` + `TRY_CAST` 最稳。

---

## 9. 建议学习路径

1. **跑通连接**：用上面的 DuckDB 片段，查出 10 个主诊断标题。
2. **画一个病人的时间线**：选一个 `subject_id`，把它的 `admissions` / `edstays` / 关键 `labevents` 按 charttime 排出来，体会"相对时间"。
3. **做一个小队列**：成人 + 经 ED 入院 + 有出院小结，统计主诉分布。
4. **解析一份出院小结**：对 `discharge.text` 用正则切段，观察标题规律与 `___` 密度。
5. **读本仓库管线**：`data/mimic-iv/data_processing/build_dataset.py` 是上述全部技巧的完整范例；配套用法见同目录《数据处理方案使用手册》。

---

## 10. 合规要点（再次强调）

- credentialed DUA **禁止把数据交给第三方**：不得提交进 git、不得发往任何公网 LLM API、不得公开分享导出物。
- 所有涉及 note 文本的解析/抽取/校验，请走**本地模型**。
- 发表论文时只能展示**聚合统计或充分去标识**的示例，不得贴出可识别的原始记录。

---

## 11. 参考资料

- MIMIC-IV 官方文档：<https://mimic.mit.edu/>
- PhysioNet 项目页：<https://physionet.org/content/mimiciv/>
- DuckDB 文档：<https://duckdb.org/docs/>
- ICD ↔ CCSR 归类：AHRQ HCUP CCSR
