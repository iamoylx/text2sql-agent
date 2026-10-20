"""
压测脚本的解析器自证（P2）。

## 为什么这一组用例比压测数字更重要

压测代码属于「计量器」。计量器错了，跑出来的每个数字都是错的，而且**看起来很正常**。
本文件钉的都是「错了也不报错、只会静默给出错误数字」的地方：

1. **分隔符是 `\\n\\n`**（`server.py:459` 裸 `StreamingResponse`），**不是** P1 的 `\\r\\n\\r\\n`。
2. **类型在 JSON 的 `type` 里**，不在 `event:` 行。
3. **跨包**：一帧可能被 TCP 切在分隔符中间。
4. **EOF 无 done**：`server.py:470-473` 的 except 不补 done。
   若按「流结束 = 完成」，error 请求会被记成「极快的成功」。

第 5 条 `test_p2_parser_rejects_p1_frames` 是**决定性断言**：只断言「能解析 P2 帧」是单边的，
喂 P1 的 `event:` + `\\r\\n\\r\\n` 帧进来必须解析不出 `done`。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_bench():
    """从路径加载 evals/bench_concurrency.py（evals 不是包，不能直接 import）。"""
    path = ROOT / "evals" / "bench_concurrency.py"
    spec = importlib.util.spec_from_file_location("bench_concurrency_p2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = _load_bench()


def _p2_stream(events: list[dict]) -> bytes:
    return b"".join(
        f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode() for e in events
    )


def _p1_stream(frames: list[tuple[str, dict]]) -> bytes:
    """P1 的真实帧形态：event: X + data: {...}，以 \\r\\n\\r\\n 分隔。"""
    out = b""
    for event, payload in frames:
        out += f"event: {event}\r\ndata: {json.dumps(payload, ensure_ascii=False)}\r\n\r\n".encode()
    return out


# ---------------------------------------------------------------- 帧解析
def test_parse_p2_frame_reads_data_only():
    frame = bench.parse_p2_frame(b'data: {"type": "done", "status": "ok"}')
    assert json.loads(frame["data"]) == {"type": "done", "status": "ok"}


def test_parse_p2_frame_ignores_event_line():
    """P2 没有 event 行；就算有也不该被当成类型来源（类型只在 JSON 的 type 里）。"""
    frame = bench.parse_p2_frame(b'event: done\ndata: {"type": "sql", "sql": "SELECT 1"}')
    assert json.loads(frame["data"])["type"] == "sql"


def test_parse_p2_frame_without_data_is_empty():
    assert bench.parse_p2_frame(b": keepalive")["data"] is None
    assert bench.parse_p2_frame(b"")["data"] is None


# ---------------------------------------------------------------- 流式解析
def test_parser_walks_full_stream_to_done():
    stream = _p2_stream([
        {"type": "thought", "step": "plan"},
        {"type": "sql", "step": "generate_sql", "sql": "SELECT COUNT(*) FROM orders"},
        {"type": "result", "rows": [], "total": 0},
        {"type": "answer", "step": "respond", "message": "共 0 笔", "status": "ok"},
        {"type": "done", "history_id": "h1", "status": "ok"},
    ])
    p = bench.P2SseParser()
    p.feed(stream)
    assert p.done == {"type": "done", "history_id": "h1", "status": "ok"}
    assert p.n_frames == 5
    assert p.n_thought == 1
    assert p.sqls == ["SELECT COUNT(*) FROM orders"]
    assert p.answer["message"] == "共 0 笔"
    ok, outcome, err = bench.classify_outcome(p)
    assert ok is True and outcome == "done" and err is None


def test_parser_keeps_last_sql_when_retried():
    """自愈会重试：`sql` 事件可能出现多次，取最后一条才是最终落地的 SQL。"""
    stream = _p2_stream([
        {"type": "sql", "sql": "SELECT bad FROM nowhere"},
        {"type": "sql", "sql": "SELECT COUNT(*) FROM orders"},
        {"type": "done", "status": "ok"},
    ])
    p = bench.P2SseParser()
    p.feed(stream)
    assert len(p.sqls) == 2
    assert p.sqls[-1] == "SELECT COUNT(*) FROM orders"


def test_parser_survives_frame_split_across_chunks():
    """TCP 不保证帧边界，逐字节喂最坏情况必须仍能拼回 done。"""
    stream = _p2_stream([
        {"type": "sql", "sql": "SELECT 1"},
        {"type": "done", "status": "degraded"},
    ])
    p = bench.P2SseParser()
    for i in range(len(stream)):
        p.feed(stream[i:i + 1])
    assert p.done is not None
    assert p.done["status"] == "degraded"
    assert p.sqls == ["SELECT 1"]


def test_parser_counts_bad_json_without_dying():
    p = bench.P2SseParser()
    p.feed(b"data: {not json\n\n")
    p.feed(b'data: {"type": "done"}\n\n')
    assert p.n_bad == 1
    assert p.done is not None


def test_p2_parser_rejects_p1_frames():
    """**决定性断言**：喂 P1 的 `\\r\\n\\r\\n` 帧，必须解析不出 `done`。

    只断言「能解析 P2 帧」是单边的——万一解析器碰巧两种都认，那条就白测了。
    两个项目的分隔符严格互斥（`\\r\\n\\r\\n` 里不含 `\\n\\n`），
    所以这里必须一刀切干净。
    """
    p1_bytes = _p1_stream([
        ("route", {"route": "cache"}),
        ("done", {"answer": "x", "cached": True}),
    ])
    p = bench.P2SseParser()
    p.feed(p1_bytes)
    assert p.done is None
    assert p.n_frames == 0           # 压根没切出帧来
    ok, outcome, _ = bench.classify_outcome(p)
    assert ok is False and outcome == "eof_no_done"


# ---------------------------------------------------------------- EOF 判定
def test_eof_without_done_is_not_success():
    """`server.py:470-473` 的 except 只塞 error 事件然后关流，不补 done。"""
    p = bench.P2SseParser()
    p.feed(_p2_stream([{"type": "thought", "step": "plan"},
                       {"type": "sql", "sql": "SELECT 1"}]))
    ok, outcome, err = bench.classify_outcome(p)
    assert ok is False
    assert outcome == "eof_no_done"
    assert "done" in err


def test_sse_error_frame_is_failure():
    p = bench.P2SseParser()
    p.feed(_p2_stream([{"type": "error", "step": "runtime",
                        "content": "OperationalError: database is locked"}]))
    ok, outcome, err = bench.classify_outcome(p)
    assert ok is False and outcome == "sse_error"
    assert "database is locked" in err


def test_error_takes_precedence_over_done():
    p = bench.P2SseParser()
    p.feed(_p2_stream([{"type": "error", "content": "boom"},
                       {"type": "done", "status": "ok"}]))
    ok, outcome, _ = bench.classify_outcome(p)
    assert ok is False and outcome == "sse_error"


# ---------------------------------------------------------------- 指标
def test_percentile_uses_nearest_rank():
    vals = [float(i) for i in range(1, 101)]
    assert bench.percentile(vals, 50) == 50
    assert bench.percentile(vals, 95) == 95
    assert bench.percentile(vals, 99) == 99
    assert bench.percentile([], 50) is None


def test_summarize_counts_failures():
    records = [
        {"ok": True, "wall_ms": 10.0, "outcome": "done"},
        {"ok": False, "wall_ms": 999.0, "outcome": "eof_no_done"},
    ]
    s = bench.summarize(records, wall_s=1.0)
    assert s["n"] == 2 and s["n_ok"] == 1
    assert s["qps"] == 1.0
    assert s["err_rate"] == 0.5
    # 失败请求的墙钟不能进分位数，否则 P99 会被超时值拉爆
    assert s["p50_ms"] == 10.0 and s["p99_ms"] == 10.0


# ---------------------------------------------------------------- 问题集
def test_goldset_excludes_security_items():
    """security 类按设计拒绝/净化，计入错误率会把「正常降级」误判成故障。"""
    items = bench.load_items()
    assert items, "goldset_s5 读不到"
    assert all(it["type"] != "security" for it in items)
    assert all(it.get("question") for it in items)
    raw = json.loads(bench.GOLDSET.read_text(encoding="utf-8"))
    assert len(items) == len(raw) - 6, "应排除恰好 6 条 security"


# ---------------------------------------------------------------- SQL 串场
def test_norm_sql_folds_whitespace_case_and_semicolon():
    assert bench._norm_sql("SELECT  COUNT(*)  FROM Orders ;") == "select count(*) from orders"
    assert bench._norm_sql("SELECT COUNT(*) FROM orders") == \
        bench._norm_sql("select count(*)\n  from orders;")


def test_sql_crosstalk_detects_foreign_gold():
    """决定性断言：拿 A 的题却产出 B 的金标 SQL，必须被记为串场。

    只断言「答对了几条」是单边的——串场时总数可能一模一样
    （都答对了，只是答的是别人的题）。必须按 id 交叉比对才算钉住。
    """
    items = [
        {"id": "a1", "gold_sql": "SELECT COUNT(*) FROM orders"},
        {"id": "b2", "gold_sql": "SELECT SUM(price) FROM items"},
    ]
    records = [
        {"qid": "a1", "sql": "SELECT COUNT(*) FROM orders"},     # 对的
        {"qid": "b2", "sql": "SELECT COUNT(*) FROM orders"},     # 串场：拿了 a1 的答案
    ]
    own, foreign = bench._sql_crosstalk(records, items)
    assert own == 1
    assert foreign == [("b2", "a1")]


def test_sql_crosstalk_zero_when_clean():
    items = [
        {"id": "a1", "gold_sql": "SELECT COUNT(*) FROM orders"},
        {"id": "b2", "gold_sql": "SELECT SUM(price) FROM items"},
    ]
    records = [
        {"qid": "a1", "sql": "SELECT COUNT(*) FROM orders"},
        {"qid": "b2", "sql": "SELECT SUM(price) FROM items"},
    ]
    own, foreign = bench._sql_crosstalk(records, items)
    assert own == 2 and foreign == []


def test_sql_crosstalk_ignores_missing_sql():
    """没拿到 SQL（degraded / blocked）不算串场，也不能算答对。"""
    items = [{"id": "a1", "gold_sql": "SELECT 1"}]
    own, foreign = bench._sql_crosstalk([{"qid": "a1", "sql": None}], items)
    assert own == 0 and foreign == []


# ---------------------------------------------------------------- 代理绕过
def test_client_kwargs_disable_env_proxy():
    """**压测必须绕开环境代理**——实测踩出来的，不是预防性洁癖。

    本机 shell 设了 `HTTP_PROXY=http://127.0.0.1:50297`（Clash）且没有 `NO_PROXY`。
    httpx 默认 `trust_env=True`，于是「压测本地服务」实际压的是代理：
    `/api/schema` 单发 22ms，10 并发 P50 直接变成 **13.5s**，
    而它只是序列化一个 8KB 的内存字典。第一版读数差点让人得出
    「FastAPI 框架层很慢」的结论——**归因错比没数据更糟**。
    """
    import httpx

    kw = bench.client_kwargs(conc=10, timeout_s=180.0)
    assert kw["trust_env"] is False, "缺 trust_env=False，请求会被环境代理接管"
    assert kw["follow_redirects"] is False

    cli = httpx.AsyncClient(**kw)
    try:
        assert cli._trust_env is False
    finally:
        import asyncio
        asyncio.run(cli.aclose())


def test_env_proxy_would_actually_hijack_requests(monkeypatch):
    """反向证明：开着 `trust_env` 时请求**确实**会被改道到代理。

    只断言「我们关掉了」是单边的——万一 httpx 哪天不再读 env，上一条就变成永真。
    实测（httpx 0.28.1）：代理挂在 transport 的 connection pool 上
    （`_pool._proxy_url`），不在 `_mounts` 的键名里（键是 URLPattern 对象）。
    直连时 pool 是普通 `ConnectionPool`，连 `_proxy_url` 属性都没有。
    """
    import httpx

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:59999")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    target = httpx.URL("http://127.0.0.1:501/whatever")
    hijacked = httpx.Client(trust_env=True)
    direct = httpx.Client(**bench.client_kwargs(conc=2, timeout_s=5.0))
    try:
        pool_proxy = getattr(hijacked._transport_for_url(target)._pool,
                             "_proxy_url", None)
        assert pool_proxy is not None, "trust_env=True 时请求应被环境代理接管"
        assert "59999" in str(pool_proxy)
        assert getattr(direct._transport_for_url(target)._pool,
                       "_proxy_url", None) is None
    finally:
        hijacked.close()
        direct.close()


# ---------------------------------------------------------------- 控制台编码
def test_markdown_row_is_console_encodable():
    """markdown 行要能打到 GBK 控制台。

    仓库有静态闸门（`test_output_encoding.py`），但它只看源码字面量；
    这条走**真实 f-string 求值结果**——若哪天表头加了 `\\u2248`/`\\u21d2`，
    静态闸门看不见，这条能抓。
    """
    s = {"qps": 12.3, "p50_ms": 10.0, "p95_ms": 20.0, "p99_ms": 30.0, "err_rate": 0.0}
    row = bench.md_row("P2-D 无 LLM", 300, 10, s, "无线程池争用迹象")
    row.encode("gbk")
    assert row.startswith("|") and row.endswith("|")


def test_cli_help_is_wellformed_and_needs_no_server():
    with pytest.raises(SystemExit) as ei:
        bench.build_parser().parse_args(["--help"])
    assert ei.value.code == 0
    with pytest.raises(SystemExit):
        bench.build_parser().parse_args(["--path", "nonexistent"])
