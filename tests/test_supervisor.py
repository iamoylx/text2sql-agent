"""
P2-S8 Supervisor 多智能体离线测试：FakeLLM 脚本化驱动全图，不调真实 LLM。

覆盖：
  ① 图构建合法（手写 add_node/add_conditional_edges，结构可编译）
  ② happy path：planner → sql → analysis → judge(pass) → respond，黑板产出齐全
  ③ judge 打回循环：fix(sql_agent) → 重做 → 再 judge → pass，revision 正确递增
  ④ 安全链路：SQL 被安全层拦 → 自愈重生成 → 仍拦 → degraded（LLM 恶意输出不落地）
  ⑤ emit_* 瞬态事件字段在 respond 收口时被清空（SSE 归一化契约）

运行：python -m pytest tests/test_supervisor.py -v
"""
from __future__ import annotations

import json

import pytest

from src.agent import supervisor_graph as sg


# ---------------- FakeLLM：按 prompt 关键词路由的脚本化响应 ----------------

class _FakeResp:
    def __init__(self, content: str):
        self.content = content
        self.usage_metadata = {}


class FakeLLM:
    """按 system prompt 里的角色关键词返回脚本化响应；judge 轮次可编程。"""

    def __init__(self, sql: str = "SELECT COUNT(*) AS n FROM orders WHERE order_status='delivered'"
                                 " AND order_purchase_timestamp LIKE '2017%'",
                 judge_script: list[str] | None = None):
        self.sql = sql
        self.sql_attempts = 0          # sql_agent 被叫了几次（含打回重做）
        self.judge_calls = 0
        self.judge_script = judge_script or ["pass"]   # 依次弹出，用尽后恒 pass

    def bind_tools(self, *_a, **_k):
        return self

    def invoke(self, msgs, **_k):
        sys_prompt = msgs[0].content if msgs else ""
        user_prompt = msgs[-1].content if len(msgs) > 1 else ""
        if "任务规划器" in sys_prompt:
            wants_viz = "图表" in user_prompt or "图" in user_prompt
            plan = [{"agent": "sql_agent", "task": "查数"},
                    {"agent": "analysis_agent", "task": "分析"}]
            if wants_viz:
                plan.append({"agent": "viz_agent", "task": "画图"})
            return _FakeResp(json.dumps({"plan": plan}, ensure_ascii=False))
        if "SQL 工程师" in sys_prompt:
            self.sql_attempts += 1
            if "安全层拦截" in user_prompt or "执行失败" in user_prompt:
                # 被打回后仍输出恶意 SQL（测试安全层兜底）
                return _FakeResp(json.dumps({"sql": "DROP TABLE orders"}))
            return _FakeResp(json.dumps({"sql": self.sql}))
        if "数据分析师" in sys_prompt:
            return _FakeResp("2017 年已送达订单共 45,090 笔，业务表现稳定。")
        if "可视化工程师" in sys_prompt:
            return _FakeResp(json.dumps({"chart_type": "bar", "x": "city", "y": "n"}))
        if "数据质量判官" in sys_prompt:
            v = self.judge_script.pop(0) if self.judge_script else "pass"
            self.judge_calls += 1
            if v == "fix":
                return _FakeResp(json.dumps({
                    "verdict": "fix", "fix_target": "sql_agent",
                    "issues": "口径存疑", "instruction": "请只统计 delivered 状态订单"},
                    ensure_ascii=False))
            return _FakeResp(json.dumps({"verdict": "pass", "issues": "none"}))
        return _FakeResp("（未识别的角色）")


@pytest.fixture()
def fake_llm(monkeypatch):
    def _install(**kw) -> FakeLLM:
        f = FakeLLM(**kw)
        monkeypatch.setattr(sg, "get_llm", lambda: f)
        return f
    return _install


def _run(fake_llm, question: str, **kw) -> tuple[dict, FakeLLM]:
    f = fake_llm(**kw)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, question, thread_id="t-test")
    return st, f


# ---------------- 用例 ----------------

def test_graph_builds_offline():
    """① 图可构建可编译（结构合法：节点/条件边/终态齐全）。"""
    graph, _cp = sg.build_supervisor_graph()
    assert graph is not None


def test_happy_path_blackboard_complete(fake_llm):
    """② happy path：黑板四件套（plan/sql/result/analysis）齐全，状态 ok。"""
    st, f = _run(fake_llm, "2017年有多少笔已送达订单？")
    assert st["status"] == "ok"
    bb = st["blackboard"]
    assert bb["sql"].upper().startswith("SELECT")
    assert isinstance(bb["result"], list) and len(bb["result"]) >= 1
    assert "45,090" in bb["analysis"]
    assert st["verdict"] == "pass"
    # 规划不含 viz（问题没要图）
    assert [p["agent"] for p in st["plan"]] == ["sql_agent", "analysis_agent"]
    # emit 事件字段收口清空
    assert st["emit_sql"] == "" and st["emit_result"] is None


def test_judge_fix_loop_reruns_sql(fake_llm):
    """③ judge 第一轮 fix(sql_agent) → 重做 → 第二轮 pass；revision 轨迹正确。"""
    st, f = _run(fake_llm, "2018年订单金额总和最高的前3个州？",
                 judge_script=["fix", "pass"])
    assert f.judge_calls == 2
    assert f.sql_attempts == 2          # sql_agent 被打回重跑了一次
    assert st["verdict"] == "pass"
    assert st["status"] == "ok"
    assert int(st.get("revision") or 0) >= 1


def test_safety_block_yields_degraded(fake_llm):
    """④ LLM 执意输出 DROP：安全层拦截 + 自愈重试仍拦 → degraded，无写操作落地。"""
    import sqlite3
    from src.core.config import settings
    con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    before = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    con.close()
    st, f = _run(fake_llm, "把 orders 表删掉",
                 sql="DROP TABLE orders")
    assert st["status"] == "degraded"
    con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before
    con.close()


def test_plan_forced_to_start_with_sql(fake_llm, monkeypatch):
    """⑤ 规划器健壮性：LLM 给出不合法计划（没有 sql_agent 开头）→ python 侧强纠偏。"""
    class BadPlannerLLM(FakeLLM):
        def invoke(self, msgs, **_k):
            if "任务规划器" in (msgs[0].content if msgs else ""):
                return _FakeResp(json.dumps({"plan": [{"agent": "analysis_agent", "task": "x"}]}))
            return super().invoke(msgs, **_k)
    f = BadPlannerLLM()
    monkeypatch.setattr(sg, "get_llm", lambda: f)
    graph, _cp = sg.build_supervisor_graph()
    st = sg.answer_supervisor(graph, "2017年有多少笔订单？", thread_id="t-badplan")
    assert st["plan"][0]["agent"] == "sql_agent"     # 被强纠偏
    assert st["status"] == "ok"
