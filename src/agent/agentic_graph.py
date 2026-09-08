"""
Agentic 工具循环状态图（P2-S4 核心，与 S3 的 react_graph 形成「两种编排」对照）。

S3 react_graph = 「确定性管线」：understand→generate→validate→execute 的顺序由代码写死，
                 LLM 只负责 generate 一步，改不了流程。适合流程固定、可解释性优先的场景。

S4 agentic_graph = 「LLM 自主编排」：同样手写 StateGraph（禁用 create_react_agent，
                 简历硬约束），但顺序由 LLM 自己决定——模型每轮输出 tool_calls，
                 图只负责「有调用→执行工具→观察回喂→再让模型想」的通用循环。
                 这更接近真实 Agent（CoT/ReAct 在模型侧，图侧只做调度与护栏）。

图结构（手写 ReAct 工具循环）：
    agent ──(有 tool_calls)──► tools ──► agent ──(有 tool_calls)──► tools ──…
      │                            ▲
      └──(无 tool_calls / 超步数 / 超预算)──► respond ──► END

  agent  节点：LLM.bind_tools(TOOL_SCHEMAS) → 返回 AIMessage（可能带 tool_calls）
  tools  节点：逐个 dispatch 工具调用，结果转 ToolMessage 回写 messages
               （错误也回写 = ReAct 的 Observe 步，模型据此自愈）
  respond 节点：终态，把模型最终回答落成 message/analysis

与 S3 共享的护栏（双保险）：
  MAX_STEPS=8    —— 工具循环步数上限（替代 S3 的 retry_count，防 LLM 无限调工具）
  TOKEN_BUDGET   —— 累计 token 上限（AGNES 免费额度保护，S3 同款）
  安全四层       —— execute 工具内部仍走 validator + mode=ro + 超时（拦截与 LLM 编排无关）
"""
from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from src.agent.nodes import _account_usage  # 复用 token 记账（跨节点单一实现）
from src.agent.state import SQLAgentState
from src.core.config import settings
from src.core.llm import get_llm
from src.tools.registry import TOOL_SCHEMAS, dispatch

# 防死循环双上限（与 S3 react_graph 同源同值）
MAX_STEPS = 8
TOKEN_BUDGET = 40000

# 数据最新完整年（相对时间兜底口径，与 S3 nodes._KNOWN_MAX_YEAR 保持一致）
_KNOWN_MAX_YEAR = 2018

# 工具结果回写时截断：结果集很大时只给模型看前 N 行，避免把上下文打爆
_OBSERVE_MAX_ROWS = 30

_AGENTIC_SYSTEM_PROMPT = f"""你是电商数据分析 Agent。数据库是 Olist 巴西电商（9 张业务表），
只能通过工具查询，禁止臆造数字。当前数据范围约 2016-09 ~ 2018-10。

## 可用工具与调用范式
1. generate_sql —— 把「业务问题」转成 SQL。⚠️ 必须先调用它拿到 SQL，再把 SQL 交给 execute 执行
   （Schema 注入在工具内部自动完成，模型无需自行记忆表结构）。
2. execute_readonly_sql —— 校验（只读/表白名单/LIMIT/超时）后执行上一步的 SQL，返回结果行。
3. compute_metric —— 对已返回的结果集做 sum/avg/count/mom/yoy/ratio 等二次指标计算。
4. render_chart —— 生成 ECharts 配置（bar/line/pie）。

## 执行纪律（每条都回答过「为什么」）
- 典型链路：generate_sql → execute_readonly_sql →（需要时）compute_metric / render_chart → 总结。
- execute 返回错误（语法错/安全拦截）时：先读懂错误原因再修正 SQL，最多重试 2 次；
  若是安全拦截（非只读/越权表/超时），立即停止并如实告知用户，绝不绕过安全策略。
- 结果为空时明确说「0 笔 / 无匹配记录」，并提示可能的时间范围或口径原因。
- 一轮只问一个问题，不要为了省步骤把多个无关查询拼进一条 SQL。

## 业务口径规则
1. 金额一律 BRL，不换算；「销售额/收入」默认含运费（price + freight_value）。
2. 订单状态默认 delivered（已送达），除非问题明确要求其他状态。
3. 相对时间且无明确年份（如「最近/今年/N个月」）用 {_KNOWN_MAX_YEAR} 年兜底并显式说明；
   用户给了明确年份则保留原年份（如 2030 就查 2030，查到空结果如实汇报）。
4. 客户维度去重/复购用 customer_unique_id（不是 customer_id）。
5. 用户点名要查的表若不在 9 张业务表内（customers/orders/order_items/order_payments/
   order_reviews/products/sellers/geolocation/product_category_translation），
   如实说明「数据库无此表」并列出现有表名即可，**立即收尾、不得再查询/展示任何近似表
   （如 users→customers），不得编造**——近似映射会误导用户以为是同一张表。

## 收尾
用中文给用户一段简洁结论：先说核心数字，再补关键发现。给出结论后不要再调用工具。
图表由 render_chart 工具独立生成（前端右栏渲染），**不要在回答文本中再粘贴
ECharts 配置或写 <echarts-config>{...}</echarts-config> 等 XML 标签**——重复且
会被前端剔除，浪费 token。回答里只写人类可读的文字与 markdown 表格。
"""


# ---------------- 节点 1：agent（LLM 自主编排，bind_tools） ----------------

def agent(state: dict) -> dict:
    question = state["question"]
    llm = get_llm()
    llm_tools = llm.bind_tools(TOOL_SCHEMAS)
    # 组装消息：SystemPrompt + 历史（含工具观察） + 用户问题置底
    msgs: list = [SystemMessage(content=_AGENTIC_SYSTEM_PROMPT)]
    hist = list(state.get("messages") or [])
    msgs += hist
    msgs.append(HumanMessage(content=question))

    # AGNES 偶发空响应 → 重试一次（P1 踩坑迁移）
    resp = None
    for _ in (1, 2):
        resp = llm_tools.invoke(msgs)
        if (getattr(resp, "content", None) or "").strip() or getattr(resp, "tool_calls", None):
            break
    upd = _account_usage(state, resp)
    upd["messages"] = [resp]                       # add_messages 追加本条 AIMessage
    upd["steps"] = int(state.get("steps") or 0) + 1
    return upd


# ---------------- 节点 2：tools（执行工具，结果回喂模型） ----------------

def _fmt_observe(result: dict) -> str:
    """把工具结果压成紧凑 JSON 回喂模型；超长 rows 截断 + 标注。"""
    out = dict(result)
    rows = out.get("rows")
    if isinstance(rows, list) and len(rows) > _OBSERVE_MAX_ROWS:
        out["rows"] = rows[:_OBSERVE_MAX_ROWS]
        out["_truncated"] = f"结果共 {len(rows)} 行，仅展示前 {_OBSERVE_MAX_ROWS} 行"
    return json.dumps(out, ensure_ascii=False, default=str)


def tools(state: dict) -> dict:
    hist = list(state.get("messages") or [])
    last = hist[-1] if hist else None
    calls = list(getattr(last, "tool_calls", None) or [])
    # 结果注册表：跨轮保留上一次 execute 的结果集（compute_metric/render_chart 的 data_ref 指向它）
    registry: dict = {"result": state.get("result") or []}
    out_msgs: list = []
    new_result = None
    seen: set[tuple[str, str]] = set()   # 去重：AGNES 偶发同一轮重复发同参数 tool_call
    for call in calls:
        name = call.get("name") or ""
        raw_args = call.get("args") or {}
        if isinstance(raw_args, str):            # 个别网关返回字符串化的 arguments
            try:
                raw_args = json.loads(raw_args)
            except Exception:
                raw_args = {}
        tool_id = call.get("id") or call.get("tool_call_id") or ""
        key = (name, json.dumps(raw_args, ensure_ascii=False, sort_keys=True))
        if key in seen:
            # 重复调用：跳过执行（同参数结果一致），也不回 ToolMessage——
            # 避免模型把"失败"误读为重试，白烧步数
            continue
        seen.add(key)
        result = dispatch(name, raw_args, registry)
        # execute 成功 → 结果集写入注册表 + 状态（供 compute_metric/render_chart 与 respond 使用）
        if name == "execute_readonly_sql" and result.get("ok"):
            registry["result"] = result.get("rows") or []
            new_result = registry["result"]
        out_msgs.append(ToolMessage(content=_fmt_observe(result),
                                    tool_call_id=tool_id, name=name))
    upd = {"messages": out_msgs}                 # add_messages 追加 ToolMessage
    if new_result is not None:
        upd["result"] = new_result
    return upd


# ---------------- 条件边路由 ----------------

def route_after_agent(state: dict) -> str:
    hist = list(state.get("messages") or [])
    last = hist[-1] if hist else None
    wants_tools = bool(last and getattr(last, "tool_calls", None))
    over_steps = int(state.get("steps") or 0) >= MAX_STEPS
    over_budget = int(state.get("token_cost") or 0) > TOKEN_BUDGET
    if wants_tools and not over_steps and not over_budget:
        return "tools"
    return "respond"


# ---------------- 节点 3：respond（终态收口） ----------------

def respond(state: dict) -> dict:
    hist = list(state.get("messages") or [])
    last = hist[-1] if hist else None
    # 正常收尾：模型已给出结论（无待办工具调用）
    if isinstance(last, AIMessage) and not getattr(last, "tool_calls", None):
        content = (last.content or "").strip()
        if content:
            rows = state.get("result") or []
            return {
                "status": "ok",
                "message": content,
                "analysis": content,
                "result": rows,
                "steps": state.get("steps"),
            }
    # 步数/预算用尽但仍想调工具 → 降级（给出最后一次工具观察作为线索）
    over = "步数上限" if int(state.get("steps") or 0) >= MAX_STEPS else "Token 预算"
    err_hint = ""
    for m in reversed(hist):
        if isinstance(m, ToolMessage) and m.content and '"ok": false' in m.content:
            err_hint = f"最后一次工具反馈: {m.content[:200]}"
            break
    return {
        "status": "degraded",
        "message": (f"已达{over}，本轮未收敛。{err_hint}\n"
                    "建议：把问题拆小或联系数据负责人确认口径。"),
        "steps": state.get("steps"),
    }


# ---------------- 图构建 ----------------

def build_agentic_graph():
    """构建并编译 agentic 工具循环图。返回 (graph, checkpointer)。"""
    checkpointer = MemorySaver()
    g = StateGraph(SQLAgentState)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("respond", respond)
    g.set_entry_point("agent")
    g.add_conditional_edges("agent", route_after_agent,
                            {"tools": "tools", "respond": "respond"})
    g.add_edge("tools", "agent")                 # 观察回喂后回 agent 再决策（循环本体）
    g.add_edge("respond", END)
    graph = g.compile(checkpointer=checkpointer)
    return graph, checkpointer


def answer_agentic(graph, question: str, thread_id: str | None = None) -> dict:
    """跑一次 agentic 问答，返回最终完整 state。thread_id 用于隔离多轮。"""
    config = {"configurable": {"thread_id": thread_id or "agentic-default"}}
    try:
        return graph.invoke({"question": question}, config=config)
    except Exception as e:
        return {
            "question": question,
            "status": "degraded",
            "message": f"执行异常：{type(e).__name__}: {str(e)[:200]}",
        }
