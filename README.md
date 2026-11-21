# 住户出行调查配权 API

基于 **FastAPI + SQLite + 原生 HiGHS 线性规划引擎** 的应答户权重校准服务，
支持 `docker compose` 一键启动。

## 业务规则

- 两份源表均可**整份换交**（历史版本保留，当前版本由指针切换）：
  - **应答名单**：唯一户号 `household_id`、区县 `district`、家庭人数档
    `size_band`、正数初始权重 `init_weight`；
  - **总体目标表**：两个维度（`district` / `size_band`）下各组的目标户数
    `target_count`。
- 配权时每户权重限制在初始权重的 **0.5～2 倍**。
- 两阶段字典序优化（均为线性规划）：
  1. **先**最小化两个维度所有组「加权户数 − 目标户数」的**绝对差之和**；
  2. **再**在保持第一阶段最优的前提下，最小化各户权重相对初始权重的
     **绝对改动之和**。
- 结果分两种：
  - **能配平**（绝对差之和为 0）：下游可领取**逐户权重**；
  - **不能配平**：不提供逐户权重，只给各组
    `difference = 加权户数 − 目标户数` 的差额，供调查员排查缺样。
- **冲突诊断**：各组单看都可能达到、合在一起却配不平时，调查员可凭
  「已完成且无法配平」的作业号，查询该一起复核的区县/人数档分组。
  诊断始终用**该作业当时的名单与总体目标快照**计算，历史作业、换交期间
  均可查询。
- 异常均带原因：缺表、缺分组目标、求解失败分别返回明确错误码与说明。
- **配权期间允许换交**：换交失败（校验不过）保留原表与既有供数；
  换交成功后旧版结果立即停供，状态回到「待配权」，进行中的旧作业完成时
  其结果因版本过时而丢弃。
- 两表尚未齐备或当前版本尚未算完时返回「待配权 / 计算中」；
  所有结果查询都**附带两表版号**。

## 启动

```bash
docker compose up -d --build
```

- 服务：`http://localhost:8000`
- 交互式文档（Swagger）：`http://localhost:8000/docs`
- 健康检查：`GET /health`
- SQLite 数据持久化在命名卷 `weighting-data`（容器内 `/data/weighting.db`）。

本地开发（Python 3.11）：

```bash
pip install -r requirements.txt
WEIGHTING_DB_PATH=./weighting.db uvicorn app.main:app --reload
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tables/respondents` | 整份换交应答名单（JSON） |
| POST | `/tables/respondents/csv` | 整份换交应答名单（CSV 上传） |
| POST | `/tables/targets` | 整份换交总体目标表（JSON） |
| POST | `/tables/targets/csv` | 整份换交总体目标表（CSV 上传） |
| GET | `/tables/versions` | 查看两表当前版号与内容哈希 |
| POST | `/weighting/jobs` | 对当前两表版本发起配权（后台异步） |
| GET | `/weighting/status` | 查询状态（缺表/待配权/计算中/已配平/无法配平/失败） |
| GET | `/weighting/weights` | 配平后领取逐户权重（支持 `limit`/`offset`） |
| GET | `/weighting/differences` | 领取各组加权户数减目标户数的差额 |
| GET | `/weighting/jobs/{job_id}/conflict-diagnosis` | 按作业号诊断无法配平的分组冲突 |

JSON 请求体形如：

```json
{ "records": [
  {"household_id": "H0001", "district": "Dongcheng",
   "size_band": "S1", "init_weight": 1.0}
]}
```

```json
{ "records": [
  {"dimension": "district", "group_name": "Dongcheng", "target_count": 6.0},
  {"dimension": "size_band", "group_name": "S1", "target_count": 10.0}
]}
```

CSV 表头见 `examples/`：

```bash
curl -X POST http://localhost:8000/tables/respondents/csv \
  -F "file=@examples/respondents.csv"
curl -X POST http://localhost:8000/tables/targets/csv \
  -F "file=@examples/targets.csv"
```

## 典型流程

```bash
# 1. 换交两表（返回各自版号）
curl -X POST localhost:8000/tables/respondents/csv -F "file=@examples/respondents.csv"
curl -X POST localhost:8000/tables/targets/csv     -F "file=@examples/targets.csv"

# 2. 发起配权
curl -X POST localhost:8000/weighting/jobs

# 3. 轮询状态
curl localhost:8000/weighting/status

# 4a. 已配平 -> 逐户权重（响应含 respondent_version / target_version）
curl localhost:8000/weighting/weights

# 4b. 无法配平 -> 权重接口返回 409，改领差额
curl localhost:8000/weighting/differences
```

## 状态机与错误码

| HTTP / error 码 | 触发场景 |
| --- | --- |
| `409 missing_table` | 两表未齐备就发起配权或查询 |
| `409 missing_group_targets` | 名单中出现的区县/人数档在目标表中没有对应目标 |
| `409 weighting_in_progress` | 当前版本已有配权作业在跑，重复发起 |
| `409 not_ready` | 当前版本尚未算完（待配权/计算中），结果暂不可领 |
| `409 result_unavailable` | 无法配平时请求逐户权重，或作业计算失败 |
| `400 invalid_payload` | 换交数据校验失败（空表、重复户号/分组、权重非正、目标为负等） |
| `404 job_not_found` | 冲突诊断时作业号不存在 |
| `409 job_status_conflict` | 作业仍在计算中 / 已配平 / 计算失败，不满足「已完成且无法配平」 |
| `409 snapshot_missing` | 作业当时使用的名单或目标表版本快照已缺失，无法按原表诊断 |

换交校验在写入前完成，失败时数据库指针不变，旧表与旧结果继续供数；
换交成功后版号前进，旧版权重/差额立即停供。

## 冲突诊断

```bash
curl localhost:8000/weighting/jobs/12/conflict-diagnosis
```

仅**已完成且状态为「无法配平」**的作业可诊断；作业号不存在、状态不符
（计算中/已配平/失败）或作业当时的源表快照缺失时，分别返回
`job_not_found` / `job_status_conflict` / `snapshot_missing` 并说明原因。

诊断不依赖当前版本指针，而是读取作业绑定的
`(respondent_version, target_version)` 两份**不可变快照**，因此历史作业、
换交期间都按该作业的原表原名单计算，响应始终附带两表版号。

### 证书原理

从总体目标表中选出若干区县集合 `A` 与若干家庭人数档集合 `B`
（**允许一边为空，名单里没有住户的组也可入选**），把对应组的加权户数
等式取正/负号相加：

```
Σ_{i: 区县∈A} w_i − Σ_{i: 人数档∈B} w_i = Σ_A 目标 − Σ_B 目标 (= 目标差值 D)
```

每户权重限初始值的 0.5～2 倍，故左端有可达上界：

- 区县、人数档**同时入选**的住户系数为 0（两边权重相抵）；
- 仅入选区县侧的住户系数 +1，最大取 `2 × 初始权重`；
- 仅入选人数档侧的住户系数 −1，最小取 `0.5 × 初始权重`。

即 `可达上界 UB = 2×W(仅区县侧) + 0.5×W(仅人数档侧)`。
若 `D − UB > 0`，超出量为正，则任何合规权重方案都不可能同时命中这些
组目标——这就是冲突证据。搜索在全部区县/人数档组合中找**超出量最大**
的一组（枚举分组数较少的维度，格雷码逐状态求值，复杂度
`O(2^|U|·|U|·|V|)`），返回所选分组、两边目标合计、目标差值、可达上界、
超出量以及上界的初始权重构成；**没有正的超出量即未找到冲突证据**。

响应示例：

```json
{
  "job_id": 12,
  "respondent_version": 1,
  "target_version": 3,
  "job_status": "unbalanced",
  "conflict_found": true,
  "selected_groups": {"districts": ["Dongcheng"], "size_bands": ["S2"]},
  "target_sum_districts": 100.0,
  "target_sum_size_bands": 1.0,
  "target_difference": 99.0,
  "reachable_upper_bound": 14.0,
  "excess": 85.0,
  "upper_bound_breakdown": {
    "both_sides_init_weight": 2.0,
    "district_only_init_weight": 7.0,
    "band_only_init_weight": 0.0,
    "neither_side_init_weight": 4.5,
    "district_only_factor": 2.0,
    "band_only_factor": 0.5
  },
  "candidate_group_counts": {"district": 4, "size_band": 3},
  "combinations_scanned": 8,
  "message": "发现冲突证据：……"
}
```

## 设计要点

- **不可变版本快照 + 当前版本指针**：每次换交写入新快照并在事务内翻转指针，
  历史版本保留可审计，失败回滚不影响在线数据。
- **结果与版本对绑定**：权重/差额按 `(respondent_version, target_version)`
  存储；后台作业落库前再次校验版本对仍为当前版本，换交导致的过期结果直接丢弃。
- **两阶段 LP（原生 HiGHS）**：用非负松弛对把绝对值线性化——
  组约束 `Σw − d⁺ + d⁻ = target`（差额即 `d⁺ − d⁻`），
  户改动用 `w − v ≤ init`、`−w − v ≤ −init`；
  第二阶段增加 `Σ(d⁺+d⁻) ≤ 第一阶段最优 + 容差` 锁定第一目标。
- 3000 户规模端到端求解约 0.1 秒。

## 测试

```bash
pip install pytest httpx
pytest -q
```

覆盖：配平/不可配平两条结果路径、0.5～2 倍权重边界、两阶段最优性、
缺表与缺分组目标、换交失败保留供数、换交成功停供旧结果、
配权进行中换交的作业过期处理、CSV 上传与分页，以及冲突诊断的
Farkas 证书搜索（含暴力枚举对照）、历史作业按原表诊断、作业号/状态/
快照缺失等异常路径，共 34 个用例。
