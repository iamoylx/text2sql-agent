# P2 Text2SQL Agent — 实测指标记录

> 用途：**所有数字必须真实跑出来**，最终变成简历数字与面试话术。
> 评测脚本：`evals/run_fewshot_ablation.py`（S2）/ `evals/run_eval.py`（S5，60 条全量）
> 数据：Olist 巴西电商公开数据集 9 表 10 万订单（SQLite dev 库 `data/db/olist.db`）

## 数据入库（P2-S1，2026-09-07）

| 表 | 行数 | 说明 |
|---|---|---|
| customers | 99,441 | 客户 |
| orders | 99,441 | 订单主表（事实表） |
| order_items | 112,650 | 订单明细（桥表） |
| order_payments | 103,886 | 支付（一单可拆多笔） |
| order_reviews | 99,224 | 评价（⚠️ 官方数据含重复 review_id，不设主键） |
| products | 32,951 | 商品 |
| sellers | 3,095 | 卖家 |
| geolocation | 1,000,163 | 邮编坐标（含重复） |
| product_category_translation | 71 | 品类葡→英翻译 |

- 时间跨度：2016-09 ~ 2018-10；状态分布：delivered 96,478 / shipped 1,107 / canceled 625 …
- dev 库 SQLite + SQLAlchemy URL 抽象（`db_url` / `db_ro_uri`），读连接 `mode=ro` 只读兜底；
  生产切 MySQL：只改连接串 + 换 `agent_ro` 只读账号（四层安全第①层）

## Schema 注入 + Few-shot 消融（P2-S2，2026-09-07）

**口径**：10 条评测金标（goldset.json，与 few-shot 库严格错题——评测卫生），SQLite 只读执行，
结果集与金标比对（忽略列名/列序/行序，浮点 round 2 位）。

| 组别 | 可执行 | 结果匹配 | 说明 |
|---|---|---|---|
| 仅 Schema 注入 | 10/10 | 6/10 = **0.600** | baseline |
| Schema + Few-shot(Top-3) | 10/10 | 10/10 = **1.000** | 增益 |
| **提升** | — | **+0.4** | 简历证据 |

- few-shot 库 `evals/few_shots.json`：12 条手工精选，覆盖 join / 日期边界 / 口径陷阱（客户去重
  用 customer_unique_id、品类过滤必须走翻译表等），全部 SQL 在库上验证可执行
- 检索器 `src/agent/fewshot.py`：中文 bigram + 业务关键词白名单的加权 TF 余弦。
  **工程取舍**：P2 架构刻意不依赖 GPU embedding 模型（轻量、可 CPU 部署）；库规模上千后
  换 BGE-M3 编码即可，接口签名不变
- few-shot 修正的 3 类错误（面试素材，A 组 4 条失败逐条归因）：
  ① 漏算运费（只 SUM(price) 不 SUM(price+freight_value)）→ few-shot 示例带运费写法
  ② 错选事实表（客单价/取消金额用 order_payments 而非 order_items，schema 注释
    「订单总额=SUM(payment_value)」有误导性）→ few-shot 的 order_items 写法纠正
  ③ 口径选择（RJ 客单价 支付口径 166.45 vs 商品口径 166.43 都合理，但问题未写清）
- ⚠️ 评测设计坑（踩过 3 个，全部修正并记录）：
  1. sqlparse 0.6.0 的 `token_first` 是**方法不是属性**——`s.token_first.match()` 抛
     AttributeError 被 except 吞掉，10 条合法 SELECT 全被误判 NOT_SELECT（首跑 0/10
     全挂但 SQL 肉眼全对 → 是评测代码 bug 不是模型 bug，修后 10/10 可执行）
  2. 金标口径必须与 system prompt 规则一致（默认只统计 delivered）——e3/e7/e10 最初
     金标未过滤状态，模型「遵守规则」却被判错；统一口径后判定才公平
  3. 口径歧义问题必须把口径写死在题干（e9 明确「客单价=order_items 商品金额含运费」），
     否则模型选支付/商品口径都合理，评测失去区分度——这正是 P2-S5「歧义表述」评测类的
     设计动机：歧义问题应触发澄清而非静默选口径

## 待测（后续 Stage）
- [x] ~~P2-S2 Schema 注入 + Few-shot 消融~~ → 0.600 → 1.000（+0.4，见上）
- [x] ~~P2-S3 ReAct 状态图 + 四层安全~~ → 8/8 行为用例通过（见下）
- [x] ~~P2-S4 工具注册 + Function Calling（agentic 图）~~ → pytest 40/40 + 行为 8/8（见下）
- [ ] P2-S5 60 条评测：SQL 执行准确率 ≥0.80 / 端到端 ≥0.75 / 自愈 ≥0.60 / 安全拦截 100%

## 工具注册 + Function Calling 双保险（P2-S4，2026-09-07）

**两种编排方式的对照（简历「手写 Agent」叙事核心）**：
- S3 `react_graph` = **确定性管线**：understand→generate→validate→execute 顺序由代码写死，
  LLM 只负责 generate 一步 → 流程固定、可解释性强。
- S4 `agentic_graph` = **LLM 自主编排**：同样手写 StateGraph（仍禁用 create_react_agent），
  但顺序由 LLM 的 tool_calls 决定——图只提供「agent→tools→回喂→agent」的通用循环 +
  MAX_STEPS=8 / TOKEN_BUDGET=40k 护栏。工具失败以 ToolMessage 回喂（ReAct 的 Observe 步），
  模型自行修正重试。
- **同一套 8 行为用例在两种图上跑都是 8/8** → 证明换编排心智不损失行为质量（no regression）。

**四工具 JSON Schema → Pydantic 双保险**（`src/tools/{models,registry}.py`）：
| 工具 | 作用 | 参数 Pydantic 校验 |
|---|---|---|
| generate_sql | question→SQL（工具内自动注入 Schema+Few-shot） | question 必填/长度 ≤500 |
| execute_readonly_sql | 校验+只读执行（四层安全在入口再次触发） | sql 必填、≤4000 字符、含 DROP/DELETE/UPDATE/ATTACH 即拒 |
| compute_metric | sum/avg/count/max/min/mom/yoy/ratio | metric ∈ 枚举白名单、value_col/data_ref 必填 |
| render_chart | 返回 ECharts 配置（S6 前端直接消费） | chart_type ∈ bar/line/pie |

**双保险原理**：LLM 侧 JSON Schema 是「软约束」（模型可能输出缺字段/错类型/幻觉参数），
服务端 Pydantic 是「硬校验」——fail_fast 一错即拒，返回参数校验失败信息回喂模型自愈。
恶意参数在类型/枚举/长度层就暴露，与 validator 的 AST 层形成纵深防御。

**实测（tests/test_tools.py 40 passed + test_safety.py 全绿）**：
- 拦截：缺必填字段 4/4、类型错/非法枚举 5/5、恶意参数（塞 DROP/DELETE/UPDATE/ATTACH/
  超长 SQL/超长 question）6/6 全拦 ✅
- 合法路径：`SELECT COUNT(*) FROM orders` 直通执行返回行；users 越权表在 Pydantic 层过了、
  被 validator 表白名单拦（blocked=True）→ 两把锁各有分工 ✅
- compute_metric 消费 results_registry 结果集：sum=60 精确 ✅

**8 行为用例 agentic 图复跑全过**（evals/run_s4_behavior.py，判准与 S3 完全一致）：
正常取数（43,428 笔）/ 多表 join Top5 州 / 2018 订单量最高月（1 月 7,069 笔）/
空结果兜底（2030 年 0 笔优雅说明）/ 恶意 DROP 净化拒绝无写落地 / SQL 自愈（品类 Top8）/
无 users 表说明（严格不映射）/ 相对时间口径默认 2018。

**本轮踩坑（面试素材）**：
1. AGNES 的 tool_calls 在 langchain 里是 `AIMessage.tool_calls` 列表——个别网关把
   `arguments` 字符串化，tools 节点要兼容 str→dict 再 dispatch（解析层健壮性）。
2. **工具结果回喂的上下文膨胀**：execute 返回上千行时全量 json 回喂会打爆上下文——tools
   节点截断到前 30 行并标注「共 N 行仅展示前 30」（_OBSERVE_MAX_ROWS），完整结果存
   state["result"] 供 respond 使用。
3. 行为用例 b7 暴露规则漏洞：模型会「说了没有 users 表」之后仍擅自映射到 customers 并
   展示数据——规则 5 收紧为「说明不存在→列出现有表→立即收尾，禁止查询/展示任何近似表」，
   严格复验 rows=0 才过。近似映射在真实 BI 里会误导用户以为是同一张表，比拒绝更危险。
4. 双图复用同一 CASES 模块（evals/run_s3_behavior.py 导出 CASES 常量）→ 口径零漂移，
   S3/S4 行为可比。

## ReAct 状态图 + 四层安全（P2-S3，2026-09-07）

**架构**：`src/agent/{state,nodes,react_graph}.py`，手写 add_node/add_conditional_edges
（禁用 create_react_agent）。图：understand → generate_sql → validate_sql → execute_sql
→(失败) self_correct 回环 / (成功或超限) respond。checkpointer=MemorySaver 支撑多轮。
防死循环双保险：retry_count 上限 3 + token_cost 预算 40k。

**四层安全（validator.py，pytest 19/19 + 攻击用例 17/17 通过）**：

| 层级 | SQLite 实现 | 验证 |
|---|---|---|
| ① 只读 | mode=ro URI（connect.py） | DELETE 被引擎拒绝 ✅ |
| ② 表名白名单 | sqlparse AST 提取 FROM/JOIN/子查询表名 ⊆ 9 表 | users / sqlite_master / 子查询绕过 / 逗号多表全拦 ✅ |
| ③ 行数限制 | 无 LIMIT 自动注入 LIMIT 1000 | orders 全表 → 1000 行 ✅ |
| ④ 语句超时 | progress_handler 每 1000 指令回调 | 百万行扫描 200ms 中断（TimeoutError）✅ |
| + 语句类型白名单 | 仅单条 SELECT（AST 层） | DELETE/UPDATE/DROP/INSERT/拼接/INTO OUTFILE 全拦 ✅ |

**8 行为用例全过**（evals/run_s3_behavior.py）：正常取数 / 多表 join / 日期边界 /
空结果兜底（2030 年 → 0 笔优雅说明）/ 恶意请求净化（模型拒绝 DROP 只查询）/ SQL 自愈 /
越权表拒绝（users → 拦截，不映射不编造）/ 相对时间口径默认 2018。

**踩坑（面试素材）**：
1. **AGNES 网关强制要求消息含 user 角色**——respond 节点只发 SystemMessage 报
   `No user query found`（400），8 用例全挂；改为 SystemMessage+HumanMessage 双角色即过。
   教训：第三方网关的隐式约束要最先摸清（P1 的空 content 是另一个网关怪癖）。
2. understand 口径改写规则 1 会把「2030年」这类**明确年份**也吞成默认口径 2018——
   规则只该兜底「最近/最新」相对时间；这是 Text2SQL 口径层的典型 over-normalize bug。
3. 恶意请求行为测试的设计误区：端到端层模型会「净化」DROP 只执行 SELECT（安全但测不到
   拦截路径）——安全层硬拦截应交给 validator 单测（17 攻击用例），端到端只验「无写操作落地」。
4. 点名不存在表时模型倾向语义映射（users→customers）——understand 提示词加
   「不在 Schema 的表不得擅自映射，需说明不存在」规则后正确拒绝。
