# text2sql-agent — 面向业务取数的数据分析 Agent

> 自然语言 → SQL → 结果 → 图表的对话式取数系统。
> **手写 LangGraph 状态图编排**（禁用 `create_react_agent`），四层只读安全，
> 60 条金标评测集 + 自愈专项，FastAPI SSE 流式 + 玻璃拟态三栏前端。

![Python](https://img.shields.io/badge/Python-3.12-blue) ![LangGraph](https://img.shields.io/badge/LangGraph-handwritten%20StateGraph-teal) ![License](https://img.shields.io/badge/license-MIT-green)

## 它解决什么问题

不会 SQL 的业务人员（运营/产品/管理者）用自然语言直接拿数字，
把「提需求 → 数据组排期 → 等回数」的长链路压缩成 30 秒问答。
业务口径（默认 delivered、客户去重用 customer_unique_id、金额含运费）写死在
system prompt，保证人人问出一致口径。

## 三种编排对照（核心设计）

| | react_graph（S3） | agentic_graph（S4） | supervisor_graph（S8） |
|---|---|---|---|
| 编排哲学 | **确定性管线**：understand→generate→validate→execute 顺序由代码写死 | **LLM 自主编排**：模型每轮输出 tool_calls，图只做「执行→观察→回喂」循环 | **规划-分工-裁决**：Planner 拆解 → 三个子 Agent（黑板共享、messages 隔离）→ Judge 质量门控 |
| 模型角色 | 只负责 generate 一步 | 自主决定调什么工具、调几轮 | 子 Agent 各司其职；Judge 有否决权（fix 打回重做，revision≤2） |
| 适合场景 | 流程固定、可解释性优先 | 开放任务、工具组合多样 | 高质量要求、可接受多次调用的成本（token 反而更低：黑板隔离上下文小） |
| 共同护栏 | MAX_STEPS + TOKEN_BUDGET + 四层安全（与编排无关，双保险） | 同左 | 同左 + judge 两层门控（Python 硬门控 + LLM-as-Judge） |

三者都手写 `add_node` / `add_conditional_edges`（禁 prebuilt）。S3/S4 同一套行为用例 8/8 双过；
S8 双跑对照：行为 7/8（唯一失败是免费额度限流）、**token -56%**、延迟 +2.8 倍（诚实代价）。
前端「Supervisor 多智能体」按钮可现场演示判官打回循环。

## 实测指标（全部真实跑出，详见 METRICS.md）

| 指标 | 数字 | 说明 |
|---|---|---|
| Few-shot 消融 | 0.600 → **1.000**（+0.4） | Schema 注入 vs +Top-3 few-shot，10 条金标 |
| SQL 生成准确率 | **0.860**（43/50） | 60 条全量评测（6 条安全题除外） |
| 端到端结果匹配 | **0.870**（47/54） | 结果集与金标比对 |
| 自愈成功率 | **0.708**（17/24） | 注入 3 类真实执行错误后恢复 |
| 安全拦截 | **6/6** | 恶意请求全拦，无写操作落地 |
| 攻击用例 | 17/17 | AST 层单测（越权表/子查询绕过/拼接/OUTFILE） |
| 写路径安全用例 | 18/18 | S9 HITL：三道闸 8 + dry-run 零副作用 3 + commit/审计 2 + CSV 导入 5 |
| Supervisor 离线用例 | 5/5 | S8：FakeLLM 驱动全图（happy path / 判官打回循环 / 安全降级 / 规划纠偏） |
| CTE 白名单用例 | 4/4 | S8 连带修复：WITH 别名不被误杀、不掩盖真越权表 |
| MCP 协议用例 | 9/9 | S7：stdio/http 双传输回环 + 安全不旁路 + 第三方工具动态注册 |
| 服务层测试 | pytest 76 passed | 工具双保险 + 安全层 + S9 写路径 + S8 Supervisor + S7 MCP（全量零回归） |

评测卫生：金标集与 few-shot 库严格错题；金标口径与 system prompt 一致（踩过 3 个评测设计坑）。
性能账本（真实计时/计费，详见 METRICS.md）：S5 全量延迟 mean 44.3s / median 37.0s / p95 101s，
**自愈的延迟代价 = 一次通过 +25%**（50.9s vs 40.7s）；supervisor **每例 token -49% 换延迟 +59%**。

## 库的增删改查（S9：HITL 提案-人审）

读路径让 Agent 自主循环；**写路径永不进自主循环**——模型只有建议权，人有否决权与执行权：

1. 写意图（正则粗筛 + LLM 严格 JSON 判定）→ 生成**提案 SQL**（仅 INSERT/UPDATE/DELETE、表白名单、UPDATE/DELETE 必带 WHERE）
2. **事务 dry-run**：BEGIN→执行→SELECT changes()→ROLLBACK，零副作用拿到精确影响行数 + 「将被改动的行」取证样本——dry-run 还能提前抓住约束违约（如 NOT NULL），拦截后转读路径兜底
3. SSE 推 `proposal` 事件 → 前端审批卡（SQL / 影响行数 / 取证样本 / 风险说明）→ 人点「确认执行 / 拒绝」
4. `/api/confirm` 复检三道闸 → **独立写凭据**（SQLite 去 mode=ro；MySQL 形态 agent_rw 账号）事务执行 → `write_audit.jsonl` 审计留痕

数据接入：左栏「＋ 导入CSV」→ 表名/列名清洗 + 类型推断 + 行数质检 → **动态注册进白名单与 Schema 注入**（重启自动恢复），Agent 下一句即可查询新表。

## MCP 对外开放 + 第三方工具接入（S7）

能力开放遵循一条原则：**MCP 只是协议外壳，安全语义不变**——

- **Server**（`src/mcp_server.py`，FastMCP）：把 generate_sql / execute_readonly_sql /
  compute_metric / render_chart 四个工具按 MCP 暴露，实现层复用 `src/tools/registry`
  同一份代码（不是绕过安全的副本）。支持 stdio 与 streamable-http 双传输：
  `python src/mcp_server.py --http 8600`
- **Client**（`src/mcp_client.py`）：后台事件循环 + AsyncExitStack 保活连接；
  任何 MCP server 一行接入：
  ```python
  from src.mcp_client import enable_mcp_tools
  enable_mcp_tools([{"name": "ext", "transport": "http", "url": "http://127.0.0.1:8610/mcp"}])
  # 发现 → inputSchema 动态转 Pydantic → register_external_tool → Agent 下一轮可见
  ```
- 经 MCP 调 `DROP TABLE` / 查白名单外表**照样被四层安全拦截**；
  与原生同名的第三方工具被跳过（原生优先），新工具经 Pydantic 校验通道 dispatch（无旁路）。

## 架构

```
用户问题
   │
   ▼
FastAPI /api/query (SSE)                     ┌────────────────────────┐
   │  producer thread + queue                │ react_graph（确定性管线）│
   ▼                                        │   understand → generate │
┌──────────────┐    ┌──────────────────┐    │   → validate → execute  │
│ 四层安全层    │───▶│ Olist 9 表        │    │   → self_correct ↺     │
│ 只读/白名单/  │    │ SQLite dev 库     │◀───┤                        │
│ LIMIT/超时   │    │ (MySQL 就绪:      │    │ agentic_graph（LLM 自主）│
└──────────────┘    │  agent_ro 只读)   │    │   agent ⇄ tools 循环    │
                    └──────────────────┘    │   → respond             │
                                            └────────────────────────┘
   事件: thought / sql / result / chart / answer / error
   ▼
三栏前端：Schema 树 │ 对话（SQL 折叠/反馈）│ 结果表 + ECharts
```

## 快速开始

```bash
# 1. 环境（Python 3.12）
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements.txt

# 2. 数据入库（Olist 公开数据集 9 表）
python scripts/init_db.py
#    建库后会自动刷新 data/schema.md（给人读的 Schema 文档，随仓库提交）
#    单独刷新文档（不重建库）：python scripts/init_db.py --dump-schema

# 3. 配置 LLM（.env）
#    AGNES_API_KEY=...   （或 DEEPSEEK_API_KEY，config 留有备用位）

# 4. 起 API 服务（8501）
python scripts/run_api.py
# 打开 http://127.0.0.1:8501

# 5. 跑评测
python evals/run_eval_s5.py          # 60 条全量
python evals/run_selfheal_eval.py    # 自愈专项
python -m pytest -q                  # 单测
```

## 代码导读（推荐阅读顺序）

> 详细版（每文件的内容/作用/模块关系/阅读要点 + 整体 pipeline 图 + 术语表）见
> **[ARCHITECTURE.md](ARCHITECTURE.md)**；下表是速览版。

| 顺序 | 文件 | 知识点 |
|---|---|---|
| 1 | `src/core/config.py` | pydantic-settings；SQLite/MySQL URL 抽象（库无关设计） |
| 2 | `src/db/connect.py` | SQLAlchemy 双引擎；SQLite `mode=ro` creator 注入（只读兜底） |
| 3 | `src/safety/validator.py` | **四层安全**：sqlparse AST 白名单 / LIMIT 注入 / progress_handler 超时 |
| 4 | `src/agent/state.py` | LangGraph State 定义（Annotated reducer） |
| 5 | `src/agent/react_graph.py` | **手写确定性状态图**（对照 prebuilt 的差异） |
| 6 | `src/agent/agentic_graph.py` | **手写 ReAct 工具循环**：bind_tools → 条件边 → 观察回喂 |
| 7 | `src/tools/registry.py` | Function Calling 工具注册：JSON Schema（软约束）+ Pydantic（硬校验）双保险 |
| 8 | `src/agent/prompting.py` + `fewshot.py` | Schema 注入 / few-shot 加权检索（bigram TF，CPU 可部署取舍） |
| 9 | `src/api/server.py` | FastAPI + SSE 真流式（producer thread + queue）；事件归一化；输出净化 |
| 10 | `web/index.html` | 原生 JS 三栏布局；SSE 消费；markdown 逐行渲染；ECharts |
| 11 | `src/mcp_server.py` + `src/mcp_client.py` | **MCP 双向**：FastMCP 工具开放（复用 registry）+ AsyncExitStack 客户端 + 动态注册 |
| 12 | `evals/run_eval_s5.py` | 评测框架：金标比对 / 断点续跑 / 结果归因 |
| 13 | `METRICS.md` | 全部指标数字与踩坑记录（面试素材库） |

## 已知边界与生产化路线（诚实清单）

数据层当前为开发期一次性导入的静态快照；生产落地还需：
企业数据链路（ETL/CDC → 分析从库，Agent 只当读者）、SSO/RBAC 行列权限与审计、
PII 脱敏、schema 变更感知与 schema linking、并发/缓存/限流、Docker 部署、
写操作走「提案-人审-执行」（LangGraph interrupt）。详见 METRICS.md 与构建历程图。

## License

MIT
