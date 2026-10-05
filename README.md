# 同行评审分派 API

基于 **Python + FastAPI + NetworkX** 的本地同行评审分派服务。输入稿件与评审人信息，
输出满足全部约束的最优分派；无解时返回最小容量缺口证明。另提供应急改派接口：
评审人退出后，对已下发的分派做尽量少改动的补齐。

## 规则说明

- **匹配分**：某 (稿件, 评审人) 组合的匹配分 = 评审人对该稿件**各主题评分之和**。
- **可用组合**：评审人与稿件无冲突，且匹配分 **> 0**；冲突或总分为 0 的组合不可用。
- **约束**：
  - 每篇稿件恰好分派 `required_reviewers` 名**不同**的评审人（同一稿件不得重复使用同一评审人）；
  - 每名评审人被指派的稿件总数不超过其 `capacity`。
- **优化顺序**（完整分派存在时）：
  1. 最小化评审人的**最大负载率**（负载率 = 已指派数 / 容量）；
  2. 在此基础上最大化**总匹配分**；
  3. 仍并列时，按 (稿件 ID, 评审人 ID) 排序后的分派列表取**字典序最小**者
     （ID 按字符串字典序比较）。
- **无解证明**：当不存在完整分派时，返回一个稿件子集 `S` 及其全部可用评审人 `N(S)`，
  满足 `S` 的需求总数 > `N(S)` 的有效容量和（由最大流最小割保证存在）。由于同一评审人
  对同一稿件只能计一次，评审人对着子集的**有效容量** = `min(容量, 子集中其可评审的稿件数)`。
  在所有违反该条件的子集中，先取**稿件数最少**者，再按稿件 ID 字典序取最小者。
- **应急改派**（`POST /reassign`）：评审人退出后，撤销其全部关系、保留其余已确认关系，
  在可行的完整改派中依次优化：
  1. **尽量保留**其余原关系（保留数量最大）；
  2. 最小化评审人最大负载率；
  3. 最大化总匹配分；
  4. 按 (稿件 ID, 评审人 ID) 取关系列表字典序最小者。

  无解时明确返回 `infeasible`，不输出部分分派。

## 快速开始（Docker Compose 一条命令）

```bash
docker compose up --build
```

服务启动后访问：

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8000/assign` | `POST` 求解分派 |
| `http://localhost:8000/reassign` | `POST` 评审人退出后的应急改派 |
| `http://localhost:8000/health` | `GET` 健康检查 |
| `http://localhost:8000/docs` | Swagger UI 交互文档 |
| `http://localhost:8000/openapi.json` | OpenAPI 规范 |

停止：`docker compose down`。

## 请求格式 `POST /assign`

```json
{
  "manuscripts": [
    {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2}
  ],
  "reviewers": [
    {"id": "r1", "scores": {"nlp": 5, "ml": 4}, "capacity": 2, "conflicts": ["m2"]}
  ]
}
```

- `manuscripts`：1～10 篇，`id` 唯一；`topics` 至少一个且不重复；`required_reviewers ≥ 1`。
- `reviewers`：1～20 名，`id` 唯一；`scores` 为 主题 → 非负评分（缺省主题按 0 计）；
  `capacity ≥ 0`；`conflicts` 为冲突稿件 ID 列表（必须引用存在的稿件）。
- 不合法的请求体会返回 `422` 及具体错误信息。

## 示例

### 可行实例

```bash
curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "manuscripts": [
      {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2},
      {"id": "m2", "topics": ["vision"], "required_reviewers": 1}
    ],
    "reviewers": [
      {"id": "r1", "scores": {"nlp": 5, "ml": 4}, "capacity": 2, "conflicts": ["m2"]},
      {"id": "r2", "scores": {"nlp": 3, "vision": 5}, "capacity": 1, "conflicts": []},
      {"id": "r3", "scores": {"ml": 4, "vision": 2}, "capacity": 1, "conflicts": []}
    ]
  }'
```

响应（`200 OK`）：

```json
{
  "status": "feasible",
  "assignments": [
    {"manuscript_id": "m1", "reviewer_id": "r1", "score": 9.0},
    {"manuscript_id": "m1", "reviewer_id": "r3", "score": 4.0},
    {"manuscript_id": "m2", "reviewer_id": "r2", "score": 5.0}
  ],
  "total_score": 18.0,
  "max_load_ratio": 1.0,
  "loads": {"r1": 1, "r2": 1, "r3": 1}
}
```

### 无解实例（返回最小容量缺口证明）

```bash
curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "manuscripts": [
      {"id": "m1", "topics": ["nlp"], "required_reviewers": 2},
      {"id": "m2", "topics": ["nlp"], "required_reviewers": 2}
    ],
    "reviewers": [
      {"id": "r1", "scores": {"nlp": 5}, "capacity": 2, "conflicts": []},
      {"id": "r2", "scores": {"nlp": 4}, "capacity": 1, "conflicts": []},
      {"id": "r3", "scores": {"nlp": 3}, "capacity": 1, "conflicts": ["m1", "m2"]}
    ]
  }'
```

响应（`200 OK`）：`{m1, m2}` 共需 4 个评审位置，但可用评审人只有 r1、r2
（r3 与两篇稿件均冲突），有效容量和为 `min(2,2) + min(1,2) = 3`，缺口为 1。

```json
{
  "status": "infeasible",
  "proof": {
    "manuscripts": ["m1", "m2"],
    "reviewers": ["r1", "r2"],
    "demand": 4,
    "capacity": 3,
    "gap": 1
  }
}
```

### 健康检查

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}
```

## 应急改派 `POST /reassign`

评审人退出后，对已下发的分派做**尽量少改动**的补齐。请求体在 `POST /assign`
相同的 `manuscripts`、`reviewers` 基础上增加三个字段：

```json
{
  "manuscripts": ["同 /assign"],
  "reviewers": ["同 /assign"],
  "original_assignments": [
    {"manuscript_id": "m1", "reviewer_id": "r1"}
  ],
  "withdrawn_reviewer_id": "r2",
  "confirmed": [
    {"manuscript_id": "m1", "reviewer_id": "r1"}
  ]
}
```

- `original_assignments`：原完整分派的 (稿件, 评审人) 关系列表；
- `withdrawn_reviewer_id`：退出评审人的 ID（必须在评审人列表中）；
- `confirmed`：原分派中**已确认、必须保留**的关系（可为空，不得涉及退出者）。

服务端会核验输入，不合法时返回 `422` 及具体错误信息：

- 原分派引用的稿件/评审人必须存在，关系不得重复；
- 原分派满足**稿件需求**（每篇稿件恰好 `required_reviewers` 名评审人）、
  **冲突**（不含冲突组合）与**容量**（每人不超过 `capacity`）约束；
- 已确认关系必须**属于原分派**，且不涉及退出者。

改派规则：

- **撤销**退出者的全部关系，其余已确认关系必须保留；
- 在可行的完整改派中依次优化：保留原关系数最大 → 最小化最大负载率 →
  最大化总匹配分 → (稿件 ID, 评审人 ID) 字典序最小；
- 无解时返回 `{"status": "infeasible", "reason": ...}`，**不输出部分分派**。

成功时返回：

- `assignments`：改派后的**完整分派**（含各组合匹配分）；
- `revoked`：相对原分派被**撤销**的关系（含退出者的全部关系）；
- `added`：相对原分派**新增**的关系；
- `loads`：每名评审人的负载（退出者为 0），以及 `total_score`、`max_load_ratio`。

### 改派示例（可行）

r2 退出；原分派为 m1←{r1, r3}、m2←{r2}，其中 m1←r1 已确认。

```bash
curl -s -X POST http://localhost:8000/reassign \
  -H 'Content-Type: application/json' \
  -d '{
    "manuscripts": [
      {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2},
      {"id": "m2", "topics": ["vision"], "required_reviewers": 1}
    ],
    "reviewers": [
      {"id": "r1", "scores": {"nlp": 5, "ml": 4}, "capacity": 2, "conflicts": ["m2"]},
      {"id": "r2", "scores": {"nlp": 3, "vision": 5}, "capacity": 1, "conflicts": []},
      {"id": "r3", "scores": {"ml": 4, "vision": 2}, "capacity": 2, "conflicts": []}
    ],
    "original_assignments": [
      {"manuscript_id": "m1", "reviewer_id": "r1"},
      {"manuscript_id": "m1", "reviewer_id": "r3"},
      {"manuscript_id": "m2", "reviewer_id": "r2"}
    ],
    "withdrawn_reviewer_id": "r2",
    "confirmed": [
      {"manuscript_id": "m1", "reviewer_id": "r1"}
    ]
  }'
```

响应（`200 OK`）：r2 的 m2←r2 被撤销，m2 改派给 r3，其余原关系全部保留。

```json
{
  "status": "feasible",
  "assignments": [
    {"manuscript_id": "m1", "reviewer_id": "r1", "score": 9.0},
    {"manuscript_id": "m1", "reviewer_id": "r3", "score": 4.0},
    {"manuscript_id": "m2", "reviewer_id": "r3", "score": 2.0}
  ],
  "revoked": [{"manuscript_id": "m2", "reviewer_id": "r2"}],
  "added": [{"manuscript_id": "m2", "reviewer_id": "r3"}],
  "total_score": 15.0,
  "max_load_ratio": 1.0,
  "loads": {"r1": 1, "r2": 0, "r3": 2}
}
```

### 改派示例（无解）

同上但 r3 容量为 1：r2 退出后，m1 的第二评审位与 m2 都只能靠 r3，
超出其容量，不存在完整改派。

```bash
curl -s -X POST http://localhost:8000/reassign \
  -H 'Content-Type: application/json' \
  -d '{
    "manuscripts": [
      {"id": "m1", "topics": ["nlp", "ml"], "required_reviewers": 2},
      {"id": "m2", "topics": ["vision"], "required_reviewers": 1}
    ],
    "reviewers": [
      {"id": "r1", "scores": {"nlp": 5, "ml": 4}, "capacity": 2, "conflicts": ["m2"]},
      {"id": "r2", "scores": {"nlp": 3, "vision": 5}, "capacity": 1, "conflicts": []},
      {"id": "r3", "scores": {"ml": 4, "vision": 2}, "capacity": 1, "conflicts": []}
    ],
    "original_assignments": [
      {"manuscript_id": "m1", "reviewer_id": "r1"},
      {"manuscript_id": "m1", "reviewer_id": "r3"},
      {"manuscript_id": "m2", "reviewer_id": "r2"}
    ],
    "withdrawn_reviewer_id": "r2",
    "confirmed": [
      {"manuscript_id": "m1", "reviewer_id": "r1"}
    ]
  }'
```

响应（`200 OK`）：

```json
{
  "status": "infeasible",
  "reason": "退出者撤销后，剩余评审人无法满足全部稿件需求"
}
```

## 实现要点

- 分派建模为二分图 b-匹配：源点 → 稿件（容量 = 所需评审数）→ 评审人（容量 = 1，
  保证同一稿件不重复使用同一评审人）→ 汇点（容量 = 评审人容量/负载率上限）。
- 用 `networkx.maximum_flow` 判定可行性，并在候选负载率上二分搜索最小可行值
  （`Fraction` 精确取整，避免浮点误差）。
- 用 `networkx.network_simplex` 求最小费用流，边权为
  `-(匹配分 × 2^M) - 2^(M-1-名次)`：匹配分项占主导，指数权重使费用最小等价于
  字典序最小，一次求解同时满足三级优化目标。
- 无解时枚举稿件子集（按子集大小、ID 字典序），找到第一个需求超过有效容量和的
  子集即为最小容量缺口证明。
- 应急改派（`/reassign`）复用同一流量模型：先从原分派删去退出者并固定已确认关系
  （抵扣相应稿件需求与评审人容量，得到残差问题），再在残差网络上依次求解——
  以费用 -1 的最小费用流求**最大保留数**；在保留数不减的前提下对负载率二分搜索
  求**最小最大负载率**；最后以「保留数 × 大权重 + 匹配分 × 2^M + 字典序指数权重」
  一次最小费用流完成三级优化。已确认部分在所有解中恒定，因此优化残差问题等价于
  优化整体问题。

## 本地开发（不用 Docker）

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload   # 服务运行在 http://localhost:8000
python -m pytest                # 运行测试（含与暴力枚举对照的随机化测试）
```

## 项目结构

```
app/
  main.py      # FastAPI 应用：/assign、/reassign、/health、自动 OpenAPI
  models.py    # 请求校验与响应模型（Pydantic）
  solver.py    # 基于 NetworkX 最大流/最小费用流的求解器（含应急改派）
tests/
  test_api.py  # 单元测试 + 暴力枚举对照的随机化测试（含应急改派）
Dockerfile
docker-compose.yml
requirements.txt        # 运行时依赖
requirements-dev.txt    # 额外测试依赖
```
