"""
Supervisor 多智能体总图（P2-S8 核心，手写 StateGraph，禁用 prebuilt）。

三种编排对照（面试叙事的第三块拼图）：
  S3 react_graph      确定性管线：流程代码写死，LLM 只做 generate 一件事
  S4 agentic_graph    LLM 自主工具循环：单 Agent 自主决定调什么工具
  S8 supervisor_graph 任务分解 + 分工 + 质量门控：Planner 拆解 → 三个子 Agent 流水执行
                      → Judge 裁决（LLM-as-Judge），不合格打回重做（revision≤2）

黑板模式（Blackboard）——「子 Agent messages 隔离 + 共享黑板」的实现口径：
  - 每个子 Agent 的 LLM 对话历史是**节点内局部变量**，不进全局 State（互相看不见
    对方的碎碎念，上下文干净、token 省）；
  - 子 Agent 只把**产出物**（SQL / 结果集 / 分析文本 / 图表配置）写进共享 blackboard；
  - Judge 只看黑板上的产出物 + 原始问题裁决——不看任何子 Agent 的内部对话。
  这与多智能体系统的两种主流协作模式（消息总线 vs 黑板）中的黑板模式同构。

图结构（全部 add_node / add_conditional_edges 手写）：
    planner ──► sql_agent ──► analysis_agent ──► viz_agent ──► judge
                                                            │
                           ┌──── verdict=fix, revision≤2 ──┘（打回指定子 Agent）
                           ▼
                        respond ──► END

质量门控（Judge 两道）：
  ① Python 硬门控：分析文本非空、结果与结论一致性抽查（有行却称无数据的矛盾检测）
  ② LLM-as-Judge 软门控：数字溯源（结论中的数字能否在结果集中找到）、口径合规
    （默认 delivered / 年份口径 / 去重键）、图表字段与结果列匹配
  verdict=fix 时必须给出 fix_target（sql/analysis/viz 之一）+ 可执行的修改指令。
"""
from __future__ import annotations

import json
import operator
import re
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from src.agent.nodes import _account_usage
from src.core.config import settings
from src.core.llm import get_llm
from src.safety.validator import execute_with_timeout, validate_sql
from src.tools.registry import dispatch

MAX_REVISION = 2          # Judge 打回重做上限（防 judge-死循环烧 token）
MAX_NODE_STEPS = 16       # 全图 LLM 调用次数上限（planner+子agent+judge 合计）
_KNOWN_MAX_YEAR = 2018    # 与 S3/S4 口径一致

# 三类子 Agent 的职责边界（prompt 里的角色卡，互相隔离）
_ROLE_CARDS = {
    "sql_agent": "SQL 工程师：只负责把数据需求变成可执行的正确 SELECT，并执行拿到结果行。",
    "analysis_agent": "数据分析师：只负责基于已有结果行写业务结论，禁止编造结果里没有的数字。",
    "viz_agent": "可视化工程师：只负责把结果行变成 ECharts 图表配置（bar/line/pie）。",
}


class SupervisorState(TypedDict, total=False):
    question: str
    plan: list[dict]                 # planner 产出 [{agent, task}]
    step_index: int                  # 当前执行到 plan 第几步
    blackboard: dict                 # 共享黑板 {sql,result,metrics,analysis,chart_config,fix_instruction}
    verdict: str                     # judge 裁决: pass | fix
    fix_target: str                  # judge 指定的重做对象
    judge_issues: str                # judge 给出的问题清单（审计留痕）
    revision: int                    # 已打回次数
    token_cost: int
    steps: int                       # 全图 LLM 调用计数（防死循环）
    status: Literal["ok", "degraded"]
    message: str
    analysis: str
    result: list[dict]
    chart_config: dict | None
    # 事件提示字段（供 SSE 归一化层转成前端事件；respond 时清空）
    emit_note: str
    emit_sql: str
    emit_result: list | None
    emit_chart: dict | None


# ---------------- 工具函数 ----------------

def _llm_json(llm, sys_prompt: str, user_prompt: str) -> tuple[dict | None, int]:
    """调 LLM 并解析严格 JSON（截取第一个 {...} 块）。

    返回 (解析结果, 本次消耗 token)。token 口径与 S3/S4 一致（usage 优先，缺失按字符估算）
    ——双跑评测要对比 token 成本，Supervisor 每个 LLM 调用都必须记账，漏记就是造假数据。
    AGNES 429 限流：退避 20s 重试一次；空响应：退避 3s 重试一次。
    """
    import time

    from src.agent.nodes import _num_tokens

    tokens = 0
    for attempt in (1, 2):
        try:
            resp = llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=user_prompt)])
        except Exception as e:  # noqa: BLE001 —— 429/网络抖动统一退避重试
            if "429" in str(e) or "RateLimit" in type(e).__name__:
                if attempt == 1:
                    time.sleep(20)
                    continue
                raise
            raise
        usage = getattr(resp, "usage_metadata", None) or {}
        tokens += int(usage.get("total_tokens") or 0) or _num_tokens(str(resp.content or ""))
        raw = (resp.content or "").strip()
        if raw:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0)), tokens
                except Exception:
                    continue
        if attempt == 1:
            time.sleep(3)
    return None, tokens


def _pay(state: dict, upd: dict, tokens: int) -> dict:
    """把 _llm_json 报告的 token 计入 state（口径对齐 _account_usage）。"""
    if tokens:
        upd["token_cost"] = int(state.get("token_cost") or 0) + tokens
    return upd


def _bump(state: dict, extra: dict | None = None) -> dict:
    """公共状态增量：LLM 调用计数 +1（防死循环护栏）。"""
    upd = {"steps": int(state.get("steps") or 0) + 1}
    if extra:
        upd.update(extra)
    return upd


def _blackboard(state: dict) -> dict:
    return state.get("blackboard") or {}


def _own_index(state: dict, name: str) -> int:
    """本子 Agent 在 plan 中的下标。子 Agent 结束时把 step_index 写成它——
    路由函数不能改 state，推进责任必须落在节点自己身上（否则原地死循环）。"""
    for i, p in enumerate(state.get("plan") or []):
        if p.get("agent") == name:
            return i
    return 0


def _fix_hint(state: dict, target: str) -> str:
    """Judge 打回时给对应子 Agent 的修改指令（没有则空串）。"""
    if state.get("fix_target") != target:
        return ""
    ins = (_blackboard(state).get("fix_instruction") or "").strip()
    return f"\n\n【质量门控打回，必须修正】{ins}" if ins else ""


# ---------------- 节点 1：planner（任务拆解） ----------------

_PLANNER_PROMPT = """你是数据分析任务规划器。把用户的业务问题拆成 2-3 步的执行计划。
子 Agent 只有三种（按此顺序执行，不可调换）：
  sql_agent       —— 查数：产出 SQL 与结果行
  analysis_agent  —— 分析：基于结果行写业务结论
  viz_agent       —— 画图：把结果行变成图表（仅当用户明确要图时加入）

规则：
1. 第一步必须是 sql_agent（没有数据就没有一切）。
2. 用户没要图就不要排 viz_agent。
3. task 字段写清楚该步的口径要求（年份/状态/去重键等）。

只输出 JSON：{"plan": [{"agent": "sql_agent", "task": "..."}, ...]}"""


def planner(state: dict) -> dict:
    llm = get_llm()
    out, tok = _llm_json(llm, _PLANNER_PROMPT,
                         f"用户问题：{state['question']}\n（数据范围约 2016-09 ~ 2018-10）")
    plan = (out or {}).get("plan") or []
    # 兜底规整：python 侧强约束（不信任 LLM 的规划）
    plan = [p for p in plan if p.get("agent") in _ROLE_CARDS]
    if not plan or plan[0].get("agent") != "sql_agent":
        plan.insert(0, {"agent": "sql_agent",
                        "task": f"查询回答问题所需的数据：{state['question']}"})
    names = " → ".join(p["agent"] for p in plan)
    return _pay(state, _bump(state, {"plan": plan, "step_index": 0, "revision": 0,
                                     "blackboard": {"notes": []},
                                     "emit_note": f"规划完成：{names}"}), tok)


# ---------------- 节点 2：sql_agent（查数 + 一次自愈） ----------------

_SCHEMA_BLOCK_CACHE: str | None = None


def _schema_block() -> str:
    """完整 Schema 注入（DDL + 中文注释 + 采样值），与 S4 generate_sql 工具内部同源。
    首跑双跑评测的教训：sql_agent prompt 只列表名不列字段，模型会猜错列归属
    （b2: 把 customer_zip_code_prefix 当 orders 的列）——能力差距就差在这块上下文。"""
    global _SCHEMA_BLOCK_CACHE
    if _SCHEMA_BLOCK_CACHE is None:
        import sqlite3

        from src.db.schema import TABLES
        from src.agent.prompting import render_schema_block
        con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        try:
            row_counts, samples = {}, {}
            for t in TABLES:
                row_counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                samples[t] = [tuple(r) for r in con.execute(f"SELECT * FROM {t} LIMIT 2")]
        finally:
            con.close()
        _SCHEMA_BLOCK_CACHE = render_schema_block(samples=samples, row_counts=row_counts)
    return _SCHEMA_BLOCK_CACHE


_SQL_AGENT_PROMPT = """你是电商数据仓库的 SQL 工程师。数据库是 SQLite 方言的 Olist 巴西电商，
数据范围约 2016-09 ~ 2018-10。把数据需求写成**单条 SELECT**。

## 库 Schema（字段归属以此为准，严禁臆造列）
{schema}

## 库里只有这 9 张表（严禁使用任何其他表名，如 olist_orders_dataset 等 Kaggle 原始文件名）
customers / orders / order_items / order_payments / order_reviews /
products / sellers / geolocation / product_category_translation
（可用 WITH ... AS 定义 CTE 临时结果集，最终主查询访问的实体表必须出自上表。）

口径规则：
1. 金额一律 BRL，「销售额/收入」默认含运费（price + freight_value）。
2. 订单状态默认 delivered，除非需求明确要求其他状态。
3. 相对时间（最近/今年）用 %s 年兜底；需求给了明确年份就用那年（哪怕查到空）。
4. 客户去重/复购用 customer_unique_id。
5. 需求中的表若不在上述 9 张内，输出 {"error": "无此表：<表名>"}。

只输出 JSON：{"sql": "SELECT ..."} 或 {"error": "..."}
""" % _KNOWN_MAX_YEAR


def sql_agent(state: dict) -> dict:
    llm = get_llm()
    bb = _blackboard(state)
    task = state["plan"][state.get("step_index") or 0]["task"]
    user_prompt = f"数据需求：{task}{_fix_hint(state, 'sql_agent')}"

    sys_prompt = _SQL_AGENT_PROMPT.replace("{schema}", _schema_block())
    plan_out, tok = _llm_json(llm, sys_prompt, user_prompt)
    plan_out = plan_out or {}
    if plan_out.get("error"):
        bb["notes"] = bb.get("notes", []) + [f"sql_agent: {plan_out['error']}"]
        return _pay(state, _bump(state, {"blackboard": bb,
                                         "emit_note": f"SQL 工程师：{plan_out['error']}"}), tok)

    sql = str(plan_out.get("sql") or "").strip().rstrip(";")
    vr = validate_sql(sql)
    if not vr.passed:
        # 安全闸拦截 → 打回自愈一次（错误回喂），再拦就如实降级
        retry, tok2 = _llm_json(llm, sys_prompt,
                                f"{user_prompt}\n\n【上一版被安全层拦截：{vr.reason}，"
                                f"只能使用这 9 张表：customers/orders/order_items/order_payments/"
                                f"order_reviews/products/sellers/geolocation/"
                                f"product_category_translation，请修正】")
        tok += tok2
        sql2 = str((retry or {}).get("sql") or "").strip().rstrip(";")
        vr2 = validate_sql(sql2)
        if not vr2.passed:
            return _pay(state, _bump(state, {"status": "degraded",
                                             "message": f"SQL 被安全层拦截：{vr.reason}"}), tok)
        sql, vr = sql2, vr2

    ok, err, rows = execute_with_timeout(str(settings.db_path), vr.sql)
    if not ok:
        # 执行错误自愈一次（与 S3/S4 的自愈同思路：错误回喂 → 重生成）
        retry, tok2 = _llm_json(llm, sys_prompt,
                                f"{user_prompt}\n\n【上一版 SQL 执行失败：{err}，请修正】")
        tok += tok2
        sql3 = str((retry or {}).get("sql") or "").strip().rstrip(";")
        vr3 = validate_sql(sql3)
        if not vr3.passed:
            return _pay(state, _bump(state, {"status": "degraded",
                                             "message": f"SQL 执行失败且修正未过校验：{err}"}), tok)
        ok, err, rows = execute_with_timeout(str(settings.db_path), vr3.sql)
        if not ok:
            return _pay(state, _bump(state, {"status": "degraded",
                                             "message": f"SQL 执行失败：{err}"}), tok)
        sql = sql3

    bb = dict(bb)
    bb["sql"] = sql
    bb["result"] = rows
    return _pay(state, _bump(state, {"blackboard": bb, "emit_sql": sql,
                                     "emit_result": rows[:30],
                                     "emit_note": f"SQL 工程师：查得 {len(rows)} 行",
                                     "step_index": _own_index(state, "sql_agent")}), tok)


# ---------------- 节点 3：analysis_agent（基于结果行写结论） ----------------

def analysis_agent(state: dict) -> dict:
    llm = get_llm()
    bb = _blackboard(state)
    rows = bb.get("result") or []
    task = state["plan"][state.get("step_index") or 0]["task"]
    rows_brief = json.dumps(rows[:20], ensure_ascii=False, default=str)

    sys_prompt = f"""{_ROLE_CARDS['analysis_agent']}
数据范围约 2016-09 ~ 2018-10。铁律：
1. 结论里的每个数字必须能在结果行里找到（或明确标注为行数合计）；禁止心算出结果集中不存在的数。
2. 结果为空就必须写明「没有匹配的记录」，并解释可能的口径/时间范围原因（如 2030 年超出数据范围、
   点名的表不存在等）。
3. 若黑板备注中有上游说明（如「无此表」），必须如实转述，不得编造该表的数据。
4. 用中文，先给核心数字结论，再给 1-3 条业务发现。"""
    notes = "；".join(bb.get("notes") or [])
    user_prompt = (f"分析需求：{task}\n结果行（JSON）：{rows_brief}"
                   + (f"\n上游备注：{notes}" if notes else "")
                   + _fix_hint(state, "analysis_agent"))

    resp = llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=user_prompt)])
    text = (resp.content or "").strip()
    bb = dict(bb)
    bb["analysis"] = text
    return _account_usage(state, resp) | _bump(state, {"blackboard": bb,
                                                       "emit_note": "分析师：结论已产出",
                                                       "step_index": _own_index(state, "analysis_agent")})


# ---------------- 节点 4：viz_agent（结果行 → 图表配置） ----------------

def viz_agent(state: dict) -> dict:
    llm = get_llm()
    bb = _blackboard(state)
    rows = bb.get("result") or []
    if not rows:
        return _bump(state, {"emit_note": "可视化：无数据行，跳过图表"})

    sys_prompt = f"""{_ROLE_CARDS['viz_agent']}
根据结果行选择最合适的图表：类别对比用 bar，趋势用 line，占比用 pie。
只输出 JSON：{{"chart_type": "bar|line|pie", "x": "类别列名", "y": "数值列名"}}"""
    user_prompt = (f"结果行（JSON）：{json.dumps(rows[:20], ensure_ascii=False, default=str)}"
                   f"{_fix_hint(state, 'viz_agent')}")
    out, tok = _llm_json(llm, sys_prompt, user_prompt)
    out = out or {}
    cfg = dispatch("render_chart", {"chart_type": out.get("chart_type", "bar"),
                                    "x": out.get("x", ""), "y": out.get("y", ""),
                                    "data_ref": "result"},
                   {"result": rows})
    if cfg.get("ok"):
        bb["chart_config"] = cfg["chart_config"]
        return _pay(state, _bump(state, {"blackboard": bb, "emit_chart": cfg["chart_config"],
                                         "emit_note": "可视化：图表已生成",
                                         "step_index": _own_index(state, "viz_agent")}), tok)
    return _pay(state, _bump(state, {"emit_note": f"可视化失败：{cfg.get('error', '?')}（不阻塞结论）",
                                     "step_index": _own_index(state, "viz_agent")}), tok)


# ---------------- 节点 5：judge（LLM-as-Judge 质量门控） ----------------

_JUDGE_PROMPT = """你是数据质量判官。审查一次数据分析的产出是否合格，只依据给定材料，不臆测。

审查清单：
1. 数字溯源：分析结论里的每个关键数字，能否在结果行 JSON 中找到对应值？（找不到 = fix）
2. 口径合规：默认 delivered 状态、年份是否正确使用（2016-09 ~ 2018-10 之外要有说明）、
   客户去重是否用 customer_unique_id。
3. 结论与数据一致：结果为空时结论是否如实说明？有数据时是否给出了结论？
4. 图表（若产出）：x/y 列名是否存在于结果行的列中。

裁决规则：
- 轻微瑕疵（如措辞、多一句废话）→ pass（别吹毛求疵）。
- 数字编造 / 口径错误 / 结论与数据矛盾 → fix，并给出 fix_target（sql_agent=数据查错了 /
  analysis_agent=结论写错了 / viz_agent=图错了）与一句可执行的修改指令。

只输出 JSON：{"verdict": "pass|fix", "fix_target": "analysis_agent",
"issues": "问题清单（pass 时写 none）", "instruction": "给重做者的修改指令"}"""


def judge(state: dict) -> dict:
    bb = _blackboard(state)
    llm = get_llm()

    # ① Python 硬门控：产出完整性（比 LLM 便宜且稳定）
    if not bb.get("sql"):
        return _bump(state, {"verdict": "fix", "fix_target": "sql_agent",
                             "judge_issues": "黑板上没有 SQL 产出",
                             "revision": int(state.get("revision") or 0) + 1,
                             "emit_note": "判官：缺 SQL 产出，打回 sql_agent"})
    if not (bb.get("analysis") or "").strip():
        return _bump(state, {"verdict": "fix", "fix_target": "analysis_agent",
                             "judge_issues": "黑板上没有分析结论",
                             "revision": int(state.get("revision") or 0) + 1,
                             "emit_note": "判官：缺分析结论，打回 analysis_agent"})

    # ② LLM-as-Judge 软门控
    material = json.dumps({
        "question": state["question"],
        "sql": bb.get("sql"),
        "rows": (bb.get("result") or [])[:20],
        "analysis": bb.get("analysis"),
        "has_chart": bool(bb.get("chart_config")),
    }, ensure_ascii=False, default=str)
    out, tok = _llm_json(llm, _JUDGE_PROMPT, material)
    out = out or {}
    verdict = out.get("verdict", "pass")
    if verdict not in ("pass", "fix"):
        verdict = "pass"
    revision = int(state.get("revision") or 0)
    upd = _pay(state, _bump(state, {"verdict": verdict,
                        "judge_issues": str(out.get("issues", ""))[:300],
                        "emit_note": f"判官裁决：{verdict}"
                                     + (f"（{out.get('issues', '')[:80]}）" if verdict == "fix" else "")}), tok)
    if verdict == "fix":
        upd["fix_target"] = out.get("fix_target") if out.get("fix_target") in _ROLE_CARDS \
            else "analysis_agent"
        upd["revision"] = revision + 1
        bb = dict(bb)
        bb["fix_instruction"] = str(out.get("instruction", ""))[:400]
        upd["blackboard"] = bb
    return upd


# ---------------- 条件边 ----------------

_AGENT_BY_NAME = {}   # 构建时填充：agent 名 → 节点函数名（plan 顺序推进）


def route_next_step(state: dict) -> str:
    """子 Agent 执行完 → plan 里有下一步就推进，否则进 judge。"""
    plan = state.get("plan") or []
    idx = int(state.get("step_index") or 0) + 1
    if idx < len(plan):
        return plan[idx]["agent"]
    return "judge"


def route_after_judge(state: dict) -> str:
    """judge 裁决路由：pass → respond；fix 且未超上限 → 打回指定子 Agent（重做后按
    plan 顺序走完剩余步骤再回 judge，由 route_next_step 统一路由）。"""
    if state.get("verdict") == "fix" and int(state.get("revision") or 0) <= MAX_REVISION \
            and int(state.get("steps") or 0) < MAX_NODE_STEPS:
        return state.get("fix_target") or "respond"
    return "respond"


# ---------------- 节点 6：respond（终态收口） ----------------

def respond(state: dict) -> dict:
    bb = _blackboard(state)
    analysis = bb.get("analysis") or ""
    rows = bb.get("result") or []
    status = state.get("status") or "ok"
    message = analysis
    if status == "degraded":
        message = state.get("message") or message
    elif state.get("verdict") == "fix":
        message = (f"（质量门控提示：{state.get('judge_issues', '')}，已达打回上限，以下为当前最优结果）\n\n"
                   + analysis)
    return {"status": status, "message": message, "analysis": analysis,
            "result": rows, "chart_config": bb.get("chart_config"),
            "emit_note": "", "emit_sql": "", "emit_result": None, "emit_chart": None}


# ---------------- 图构建 ----------------

def build_supervisor_graph():
    """构建并编译 Supervisor 多智能体图。返回 (graph, checkpointer)。"""
    checkpointer = MemorySaver()
    g = StateGraph(SupervisorState)
    g.add_node("planner", planner)
    g.add_node("sql_agent", sql_agent)
    g.add_node("analysis_agent", analysis_agent)
    g.add_node("viz_agent", viz_agent)
    g.add_node("judge", judge)
    g.add_node("respond", respond)

    g.set_entry_point("planner")
    # planner → 计划第一步（plan[0] 恒为 sql_agent，python 侧已强约束）
    g.add_conditional_edges("planner", lambda s: (s.get("plan") or [{}])[0].get("agent", "sql_agent"),
                            {"sql_agent": "sql_agent", "analysis_agent": "analysis_agent",
                             "viz_agent": "viz_agent", "judge": "judge"})
    # 子 Agent 之间按 plan 顺序推进（各自条件边 → route_next_step 统一路由）
    for node in ("sql_agent", "analysis_agent", "viz_agent"):
        g.add_conditional_edges(node, route_next_step,
                                {"sql_agent": "sql_agent", "analysis_agent": "analysis_agent",
                                 "viz_agent": "viz_agent", "judge": "judge"})
    # judge：pass → respond；fix → 打回指定子 Agent（重做后再回 judge）
    g.add_conditional_edges("judge", route_after_judge,
                            {"sql_agent": "sql_agent", "analysis_agent": "analysis_agent",
                             "viz_agent": "viz_agent", "respond": "respond"})
    g.add_edge("respond", END)
    graph = g.compile(checkpointer=checkpointer)
    return graph, checkpointer


def answer_supervisor(graph, question: str, thread_id: str | None = None) -> dict:
    """跑一次 Supervisor 问答，返回最终完整 state。thread_id 用于隔离多轮。"""
    config = {"configurable": {"thread_id": thread_id or "supervisor-default"}}
    try:
        return graph.invoke({"question": question}, config=config)
    except Exception as e:
        return {
            "question": question,
            "status": "degraded",
            "message": f"执行异常：{type(e).__name__}: {str(e)[:200]}",
        }
