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
- [ ] P2-S3 ReAct 状态图 + 四层安全（sqlparse 表/语句白名单、LIMIT 注入、超时、只读）
- [ ] P2-S5 60 条评测：SQL 执行准确率 ≥0.80 / 端到端 ≥0.75 / 自愈 ≥0.60 / 安全拦截 100%
