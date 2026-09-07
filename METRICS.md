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
- [ ] P2-S5 60 条评测：SQL 执行准确率 ≥0.80 / 端到端 ≥0.75 / 自愈 ≥0.60 / 安全拦截 100%

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
