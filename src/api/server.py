"""
P2-S6 服务化：FastAPI + SSE 流式问答服务。

接口（对应简历原文）：
  POST /api/query    问一句业务问题 → SSE 流（thought/sql/result/chart/answer/error）
  GET  /api/schema   返回 9 表 Schema（表注释 + 字段中文注释，供左栏渲染）
  GET  /api/history/{id}   查询历史会话（含事件轨迹）
  POST /api/feedback 对一次问答打标（好评/差评 + 备注，沉淀评测样本）

事件协议（data: {json}\n\n 每行一个事件）：
  thought —— ReAct 思考过程（understand 口径 / 工具调用意图 / 校验结果）
  sql     —— 生成的 SQL（前端可展开 + 复制）
  result  —— 结果集（前 30 行预览 + 总数，供右栏表格/图表）
  chart   —— ECharts 配置（S4 render_chart 工具产出，前端直接 setOption）
  answer  —— 最终结论（LLM 解读文本）
  error   —— 失败/安全拦截

设计要点：
  - 双图可选（?graph=agentic|react，默认 agentic 自主工具循环）——与 S3/S4 双编排
    对照叙事一致；同一 SSE 归一化层把不同图的节点更新映射成统一事件。
  - thread_id 隔离多轮会话（checkpointer MemorySaver 按 thread 存 messages，
    实现「那 2018 年呢」式追问）。
  - 历史落盘 data/db/service.db（sqlite，纯本地，不入 git）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.core.config import PROJECT_ROOT, settings

app = FastAPI(title="Text2SQL Agent API", version="0.6.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 本地演示前端；生产收紧
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 历史存储（data/db/service.db，纯本地 sqlite）
# ---------------------------------------------------------------------------
_SVC_DB = Path(settings.db_path).parent / "service.db"


def _svc_conn() -> sqlite3.Connection:
    con = sqlite3.connect(_SVC_DB)
    con.row_factory = sqlite3.Row
    return con


def _svc_init() -> None:
    with _svc_conn() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS history(
                 id TEXT PRIMARY KEY, question TEXT, events TEXT,
                 answer TEXT, status TEXT, graph TEXT, created_at TEXT)"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS feedback(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 history_id TEXT, rating INTEGER, comment TEXT,
                 created_at TEXT)"""
        )


_svc_init()


def _save_history(rid: str, question: str, events: list[dict],
                  answer: str, status: str, graph: str) -> None:
    with _svc_conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?)",
            (rid, question, json.dumps(events, ensure_ascii=False),
             answer, status, graph,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def _load_history(rid: str) -> dict | None:
    with _svc_conn() as con:
        row = con.execute("SELECT * FROM history WHERE id=?", (rid,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"], "question": row["question"],
        "events": json.loads(row["events"]), "answer": row["answer"],
        "status": row["status"], "graph": row["graph"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500, description="业务问题")
    thread_id: str | None = Field(None, max_length=64, description="会话ID（追问复用）")
    graph: str = Field("agentic", description="agentic | react")


class FeedbackRequest(BaseModel):
    history_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: str = Field("", max_length=500)


# ---------------------------------------------------------------------------
# 图缓存（懒构建：首次请求才 import 图模块，服务启动不烧 token）
# ---------------------------------------------------------------------------
_GRAPHS: dict[str, Any] = {}


def _get_graph(graph_type: str):
    if graph_type not in _GRAPHS:
        if graph_type == "react":
            from src.agent.react_graph import build_agent_graph
            _GRAPHS["react"] = build_agent_graph()
        else:
            from src.agent.agentic_graph import build_agentic_graph
            _GRAPHS["agentic"] = build_agentic_graph()
    return _GRAPHS[graph_type]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# 防御：模型偶尔在 answer 文本里塞 <echarts-config>{...}</echarts-config>（图表已由
# render_chart 工具独立产出），同时兼容被前端 HTML 转义后的 &lt;…&gt; 形式
_CHART_XML_RE = re.compile(
    r"<\s*echarts(?:-config)?[^>]*>.*?</\s*echarts(?:-config)?\s*>", re.S | re.I)
_CHART_XML_ESC_RE = re.compile(
    r"&lt;\s*echarts(?:-config)?[^&]*&gt;.*?&lt;/\s*echarts(?:-config)?\s*&gt;", re.S | re.I)


def _strip_chart_xml(s: str | None) -> str:
    if not s:
        return s or ""
    s = _CHART_XML_RE.sub("", s)
    s = _CHART_XML_ESC_RE.sub("", s)
    return s.strip()


def _ev(type_: str, **data: Any) -> dict:
    return {"type": type_, "ts": _utc(), **data}


# ---------------------------------------------------------------------------
# SSE 事件归一化：两种图的节点更新 → 统一 6 类事件
# ---------------------------------------------------------------------------
def _rows_preview(rows: list, limit: int = 30) -> dict:
    return {"rows": rows[:limit], "total": len(rows),
            "truncated": len(rows) > limit}


def _norm_agentic(node: str, upd: dict) -> list[dict]:
    """agentic 图：agent（LLM 决策）→ tools（工具执行）→ respond（终态）。"""
    out: list[dict] = []
    msgs = upd.get("messages") or []
    for m in msgs:
        kind = type(m).__name__
        if kind == "AIMessage":
            calls = list(getattr(m, "tool_calls", None) or [])
            if calls:
                for c in calls:
                    name = c.get("name", "")
                    try:
                        args = (json.loads(c["args"]) if isinstance(c.get("args"), str)
                                else c.get("args") or {})
                    except Exception:
                        args = {}
                    if name == "generate_sql" and args.get("sql"):
                        out.append(_ev("sql", sql=args["sql"], step="agent:generate_sql"))
                    elif name == "execute_readonly_sql":
                        # 模型可能跳过 generate_sql 直接给 SQL 执行——同样展示
                        if args.get("sql"):
                            out.append(_ev("sql", sql=args["sql"], step="agent:execute"))
                        out.append(_ev("thought", step="agent:execute",
                                       content=f"准备执行 SQL，观察结果…"))
                    elif name == "render_chart":
                        out.append(_ev("thought", step="agent:render_chart",
                                       content=f"按结果生成图表…"))
            # 无 tool_calls 的 AIMessage 是模型自发的回答，留给 respond 节点统一吐 answer
        elif kind == "ToolMessage":
            tool = getattr(m, "name", "") or ""
            try:
                payload = json.loads(m.content or "{}")
            except Exception:
                payload = {}
            if tool == "execute_readonly_sql":
                if payload.get("ok"):
                    rows = payload.get("rows") or []
                    preview = _rows_preview(rows)
                    if payload.get("_truncated"):
                        preview["note"] = payload["_truncated"]
                    out.append(_ev("result", step="execute_readonly_sql", **preview))
                else:
                    err = payload.get("error", "执行失败")
                    blocked = bool(payload.get("blocked"))
                    out.append(_ev("error" if blocked else "thought",
                                   step="execute_readonly_sql",
                                   content=err, blocked=blocked))
            elif tool == "render_chart" and payload.get("ok"):
                out.append(_ev("chart", step="render_chart",
                               config=payload.get("chart_config")))
            elif tool == "compute_metric" and payload.get("ok"):
                out.append(_ev("thought", step="compute_metric",
                               content=f"指标 {payload.get('metric')} = {payload.get('value')}"))
            elif tool == "generate_sql":
                pass  # SQL 已在 agent 的 tool_call 阶段推送，避免重复
    if "message" in upd:
        out.append(_ev("answer", step="respond", message=_strip_chart_xml(upd["message"]),
                       status=upd.get("status", "ok"),
                       **_rows_preview(upd.get("result") or [])))
    return out


def _norm_react(node: str, upd: dict) -> list[dict]:
    """react 图：确定性管线节点逐个映射。"""
    out: list[dict] = []
    if node == "understand":
        cq = upd.get("clarified_question")
        if cq:
            out.append(_ev("thought", step="understand",
                           content=f"口径理解：{cq}"))
    elif node == "generate_sql":
        if upd.get("sql"):
            out.append(_ev("sql", step="generate_sql", sql=upd["sql"]))
    elif node == "validate_sql":
        err = upd.get("sql_error")
        out.append(_ev("thought", step="validate_sql",
                       content=(f"SQL 校验未过：{err}" if err else "SQL 校验通过（白名单/只读/LIMIT）")))
    elif node == "execute_sql":
        if upd.get("result") is not None:
            out.append(_ev("result", step="execute_sql",
                           **_rows_preview(upd["result"])))
        if upd.get("sql_error"):
            out.append(_ev("thought", step="execute_sql",
                           content=f"执行失败：{upd['sql_error']}（触发自愈）",
                           retry=upd.get("retry_count", 0)))
    elif node == "self_correct":
        if upd.get("sql"):
            out.append(_ev("sql", step=f"self_correct(第{upd.get('retry_count', 0)}次)", sql=upd["sql"]))
    elif node == "respond":
        out.append(_ev("answer", step="respond", message=_strip_chart_xml(upd.get("message", "")),
                       status=upd.get("status", "ok"),
                       **_rows_preview(upd.get("result") or [])))
    return out


_NORM = {"agentic": _norm_agentic, "react": _norm_react}


# ---------------------------------------------------------------------------
# SSE 生成器（生产者线程逐事件推队列 → 异步消费者逐帧 yield，真流式）
# ---------------------------------------------------------------------------
def _stream(req: QueryRequest) -> AsyncIterator[str]:
    import asyncio
    import queue
    import threading

    graph, _cp = _get_graph(req.graph)
    rid = uuid.uuid4().hex[:12]
    # 每次请求独立 thread_id：图带 MemorySaver checkpoint，固定 "default" 会让
    # 上一轮未收敛的现场（messages/steps）串到下一次查询 → 秒回"已达步数上限"
    config = {"configurable": {"thread_id": req.thread_id or rid}}
    norm = _NORM[req.graph]
    q: "queue.Queue[str | None]" = queue.Queue(maxsize=128)

    def produce() -> None:
        events: list[dict] = []
        try:
            for chunk in graph.stream({"question": req.question},
                                       config=config,
                                       stream_mode="updates"):
                for node, upd in chunk.items():
                    if not isinstance(upd, dict):
                        continue
                    for ev in norm(node, upd):
                        events.append(ev)
                        q.put(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
            answer_ev = next((e for e in reversed(events) if e["type"] == "answer"),
                             None)
            done = {"type": "done",
                    "history_id": rid,
                    "status": (answer_ev or {}).get("status", "ok")}
            q.put(f"data: {json.dumps(done, ensure_ascii=False)}\n\n")
            # 落盘（含事件轨迹，/api/history/{id} 可回放）
            _save_history(rid, req.question, events,
                          (answer_ev or {}).get("message", ""),
                          done["status"], req.graph)
        except Exception as e:  # noqa: BLE001 —— 任何异常都要转 error 事件兜住
            ev = _ev("error", step="runtime",
                     content=f"{type(e).__name__}: {str(e)[:200]}")
            q.put(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
        finally:
            q.put(None)

    async def agen() -> AsyncIterator[str]:
        threading.Thread(target=produce, daemon=True).start()
        while True:
            piece = await asyncio.to_thread(q.get)
            if piece is None:
                break
            yield piece

    return agen()


@app.post("/api/query")
async def api_query(req: QueryRequest):
    if req.graph not in _NORM:
        raise HTTPException(400, "graph 必须是 agentic 或 react")
    return StreamingResponse(_stream(req), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/schema")
def api_schema():
    from src.db.schema import TABLES
    return {
        "tables": [
            {"name": t, "comment": meta.get("comment", ""),
             "ddl": meta.get("ddl", ""),
             "columns": [{"name": c, "comment": v} for c, v in
                         (meta.get("columns") or {}).items()]}
            for t, meta in TABLES.items()
        ]
    }


@app.get("/api/history/{rid}")
def api_history(rid: str):
    row = _load_history(rid)
    if row is None:
        raise HTTPException(404, "history not found")
    return row


@app.post("/api/feedback")
def api_feedback(fb: FeedbackRequest):
    with _svc_conn() as con:
        con.execute(
            "INSERT INTO feedback(history_id, rating, comment, created_at) VALUES (?,?,?,?)",
            (fb.history_id, fb.rating, fb.comment, _utc()))
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "graph_cache": list(_GRAPHS.keys())}


# 静态前端（三栏页面），挂最后避免遮蔽 API 路由
app.mount("/", StaticFiles(directory=PROJECT_ROOT / "web", html=True), name="web")
