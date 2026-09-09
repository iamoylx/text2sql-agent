"""
P2-S7 MCP 集成测试：真实协议回环（stdio 子进程拉起 MCP server），不调 LLM。

覆盖：
  ① 协议回环：bridge → stdio 子进程 → initialize → list_tools 发现 ≥4 个工具
  ② 安全不旁路：经 MCP 调 execute_readonly_sql("DROP TABLE orders") 必被拦截；
     白名单外表同样拦——MCP 只是协议外壳，安全语义不变
  ③ 正常查询回环：SELECT COUNT(*) 经 MCP 执行返回结果行
  ④ 动态注册：与原生同名的工具被跳过（原生优先）；新名字工具注册成功且
     dispatch() 可调（Pydantic 校验通道生效：缺必填参数返回校验错误）
  ⑤ schema 适配：MCP inputSchema → OpenAI schema / Pydantic 模型转换正确性

运行：python -m pytest tests/test_mcp.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "mcp_server.py"


@pytest.fixture(scope="module")
def bridge():
    from src.mcp_client import MCPToolBridge
    b = MCPToolBridge()
    b.connect_stdio("p2", sys.executable, [str(SERVER)])
    yield b


# ---------------- ① 协议回环 ----------------

def test_list_tools_discovers_four(bridge):
    tools = bridge.list_tools("p2")
    names = {t.name for t in tools}
    assert {"generate_sql", "execute_readonly_sql",
            "compute_metric", "render_chart"} <= names
    for t in tools:
        assert t.inputSchema.get("type") == "object"
        assert t.description  # 每个工具都有描述（模型选工具的依据）


# ---------------- ② 安全不旁路 ----------------

def test_mcp_drop_table_blocked(bridge):
    out = bridge.call("p2", "execute_readonly_sql", {"sql": "DROP TABLE orders"})
    assert out.get("ok") is False
    assert "安全拦截" in out.get("error", "") or "仅允许" in out.get("error", "")


def test_mcp_nonwhitelist_table_blocked(bridge):
    out = bridge.call("p2", "execute_readonly_sql",
                      {"sql": "SELECT * FROM secret_table"})
    assert out.get("ok") is False
    assert "越权" in out.get("error", "") or "白名单" in out.get("error", "")


# ---------------- ③ 正常查询回环 ----------------

def test_mcp_select_roundtrip(bridge):
    out = bridge.call("p2", "execute_readonly_sql",
                      {"sql": "SELECT COUNT(*) AS n FROM orders"})
    assert out.get("ok") is True
    assert out["rows"][0]["n"] > 0


def test_mcp_generate_sql_returns_sql(bridge):
    """唯一依赖真实 LLM 的用例：AGNES 免费额度 429 时 skip（与 S8 评测口径一致）。"""
    out = bridge.call("p2", "generate_sql", {"question": "2017年有多少笔订单？"})
    if out.get("ok") is False and ("429" in out.get("error", "")
                                   or "限流" in out.get("error", "")):
        pytest.skip("LLM 免费额度 429 限流，非代码缺陷")
    assert out.get("ok") is True
    assert out["sql"].upper().lstrip().startswith("SELECT")


# ---------------- ④ 动态注册 ----------------

@pytest.fixture()
def _fresh_registry():
    """注册测试会改全局 registry，用后恢复快照。"""
    from src.tools import registry
    snap_schemas = list(registry.TOOL_SCHEMAS)
    snap_models = dict(registry.PARAM_MODELS)
    snap_disp = dict(registry._EXTERNAL_DISPATCH)
    yield
    registry.TOOL_SCHEMAS[:] = snap_schemas
    registry.PARAM_MODELS.clear()
    registry.PARAM_MODELS.update(snap_models)
    registry._EXTERNAL_DISPATCH.clear()
    registry._EXTERNAL_DISPATCH.update(snap_disp)


def test_register_duplicate_native_skipped(_fresh_registry):
    from src.tools.registry import register_external_tool, TOOL_SCHEMAS
    n0 = len(TOOL_SCHEMAS)
    schema = {"type": "function",
              "function": {"name": "execute_readonly_sql", "description": "dup",
                           "parameters": {"type": "object", "properties": {}}}}
    assert register_external_tool(schema, {"type": "object", "properties": {}},
                                  lambda a: {}) is None
    assert len(TOOL_SCHEMAS) == n0          # 原生优先，不覆盖


def test_register_new_tool_and_dispatch(_fresh_registry):
    """第三方新工具：注册 → Pydantic 校验 → 转发 → 结果回填；缺必填参数被校验拦下。"""
    from src.tools.registry import dispatch, register_external_tool, TOOL_SCHEMAS
    n0 = len(TOOL_SCHEMAS)
    schema = {"type": "function",
              "function": {"name": "mcp_echo", "description": "回显测试",
                           "parameters": {"type": "object",
                                          "properties": {"text": {"type": "string"}},
                                          "required": ["text"]}}}
    got = {}
    def fake_dispatch(args: dict) -> dict:
        got.update(args)
        return {"ok": True, "echo": args["text"]}
    name = register_external_tool(schema, schema["function"]["parameters"], fake_dispatch)
    assert name == "mcp_echo"
    assert len(TOOL_SCHEMAS) == n0 + 1      # agent 下一轮 bind_tools 可见

    out = dispatch("mcp_echo", {"text": "hello"})
    assert out == {"ok": True, "echo": "hello"} and got["text"] == "hello"

    bad = dispatch("mcp_echo", {})           # 缺必填 → Pydantic 校验拦截（不旁路）
    assert bad["ok"] is False and "参数校验失败" in bad["error"]


# ---------------- ⑤ schema 适配 ----------------

def test_json_schema_to_pydantic():
    from src.tools.models import json_schema_to_pydantic
    import pydantic
    M = json_schema_to_pydantic("t", {
        "type": "object",
        "properties": {"q": {"type": "string"}, "k": {"type": "integer"},
                       "r": {"type": "number"}},
        "required": ["q"],
    })
    inst = M(q="x")                       # 可选字段缺省合法
    assert inst.q == "x" and inst.k is None
    with pytest.raises(pydantic.ValidationError):
        M(k=1)                            # 必填缺失报错


def test_mcp_to_openai_schema_shape():
    from src.mcp_client import _mcp_to_openai_schema

    class FakeTool:
        name = "my_tool"
        description = "  do things  "
        inputSchema = {"type": "object", "properties": {"a": {"type": "string"}}}

    s = _mcp_to_openai_schema(FakeTool())
    assert s["type"] == "function"
    assert s["function"]["name"] == "my_tool"
    assert s["function"]["description"] == "do things"   # strip 生效
    assert s["function"]["parameters"]["type"] == "object"
