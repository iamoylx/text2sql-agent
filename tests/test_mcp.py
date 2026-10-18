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
    """唯一依赖真实 LLM 的用例：AGNES 免费额度不可用时 skip（与 S8 评测口径一致）。

    ★ 这里必须处理**两种**失败形态，而不只是一种（踩过）：
      · 网关返回 429 / 限流 → bridge.call 正常返回 dict，错误在 `error` 字段里；
      · 免费档排队过久 → `_submit` 的 `.result(timeout)` 抛 TimeoutError，**不是 dict**，
        会直接冒泡成测试失败。
    此前只判了第一种，于是限流时测试报 TimeoutError 红而非 skip——
    在「全量零回归」的口径下这属于**评测假象**（同 P1 的「评测代码 bug 先于模型怀疑」）。
    另外把超时放宽到 120s：免费档「慢但能用」不该被误判成失败。
    """
    try:
        out = bridge.call("p2", "generate_sql",
                          {"question": "2017年有多少笔订单？"}, timeout=120)
    except TimeoutError:
        pytest.skip("LLM 免费额度排队超时（>120s），非代码缺陷")
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


# ---------------- ⑥ 子进程凭据传递（零 LLM 消耗的回归防线）----------------
#
# 背景（真实踩坑）：MCP stdio 子进程的环境不是父进程 os.environ 的复制——SDK 只透传
# get_default_environment() 的白名单（本机实测仅 12 个变量）。切到 DeepSeek 后子进程
# 报 "Missing credentials"，根因在**传参层**而非密钥配置层。
# 之前 AGNES 能跑只是巧合：子进程的 settings 会读 CWD 的 .env，而 .env 正好有
# AGNES_API_KEY 兜底；换成 deepseek 后 .env 里没有对应项，兜底失效才暴露出来。
#
# ★ 本组用例**不依赖运行环境**：全部用 monkeypatch 伪造 settings 字段来驱动，
#   不读真实 os.environ / .env。这样无论谁在哪台机器上跑，结论都一致
#   （旧写法靠 `os.environ.get(...)` 判断后再 skip，属于「红绿取决于机器」的测试）。

def test_child_env_takes_value_from_settings_not_environ(monkeypatch):
    """★ 核心用例：同时给环境变量和 settings 一个**不同的**值，子进程必须拿到 settings 的。

    这是「取值源是 settings 而不是 os.environ」唯一的决定性证据——
    只断言「值等于 settings」还不够（两者恰好相同时它也会通过），
    必须让两个来源冲突，才能证明读的是哪一个。
    """
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-FROM-ENV")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-FROM-SETTINGS")

    assert _child_env()["DEEPSEEK_API_KEY"] == "sk-FROM-SETTINGS"


def test_child_env_passes_agnes_key_even_when_provider_is_deepseek(monkeypatch):
    """两家的 key 都传：换 provider 只需改 LLM_PROVIDER，不必再动传参层。"""
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setattr(settings, "agnes_api_key", "ag-from-settings")
    monkeypatch.setattr(settings, "deepseek_api_key", "ds-from-settings")
    env = _child_env()
    assert env["AGNES_API_KEY"] == "ag-from-settings"
    assert env["DEEPSEEK_API_KEY"] == "ds-from-settings"


def test_child_env_omits_empty_settings_value(monkeypatch):
    """settings 里是空串 → 该变量**不出现**，而不是传一个空串过去。

    `AGNES_API_KEY=""` 与「没有这个变量」对子进程语义不同：前者会被当成
    「配置了但为空」，后者才让它走自己的默认/报错路径。
    """
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setattr(settings, "agnes_api_key", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    env = _child_env()
    assert "AGNES_API_KEY" not in env
    assert "DEEPSEEK_API_KEY" not in env


def test_child_env_non_secret_configurable_values_are_forwarded(monkeypatch):
    """非机密的行为参数也要传：否则父子进程默认值不同 → 同一问题两条通道生成的 SQL 不同。"""
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setattr(settings, "llm_max_tokens", 777)
    monkeypatch.setattr(settings, "llm_temperature", 0.5)
    env = _child_env()
    assert env["LLM_MAX_TOKENS"] == "777"      # int → 环境变量必须是 str
    assert env["LLM_TEMPERATURE"] == "0.5"


def test_whitelist_fields_all_exist_on_settings():
    """白名单里的每个字段名必须真的存在于 Settings 上。

    ★ 这条不是形式主义：`_child_env` 用 `getattr(settings, field, None)` 取值，
    **字段名写错会静默退化成「不传」**——于是又回到「子进程缺凭据」那个原始故障，
    而且不报任何错。这条用例就是给静默失败装上报警器。
    """
    from src.core.config import Settings
    from src.mcp_client import _MCP_ENV_FROM_SETTINGS

    fields = set(Settings.model_fields)
    unknown = sorted(f for f in _MCP_ENV_FROM_SETTINGS.values() if f not in fields)
    assert not unknown, f"白名单引用了 Settings 上不存在的字段：{unknown}"


def test_child_env_keeps_safe_baseline_and_no_secret_smuggling(monkeypatch):
    """边界断言：安全基线（PATH）要在，非白名单密钥不进，写库口令不进。

    这是「修好功能」与「不放大泄露面」之间的分界线——
    全量透传也能让功能工作，但那等于把父进程所有密钥交给子进程。
    """
    from src.mcp_client import _MCP_ENV_FROM_SETTINGS, _child_env

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-should-not-leak")
    env = _child_env()
    assert env.get("PATH"), "PATH 必须有，否则子进程起不来"        # SDK 基线仍在
    assert "TAVILY_API_KEY" not in env, "非白名单密钥不得进子进程"
    assert "TAVILY_API_KEY" not in _MCP_ENV_FROM_SETTINGS, "白名单本身也不该收编无关密钥"
    assert "SCENARIO" not in _MCP_ENV_FROM_SETTINGS, "SCENARIO 无 settings 字段且无人消费，属死配置"
    # 写库口令不能进子进程：读路径的子进程没有写权限的必要。
    # 检查的是**值**而不只是键名——DB_URL 里若嵌了 agent_rw 的口令，键名是看不出来的。
    leaked = [k for k, v in env.items() if "agent_rw" in str(v)]
    assert not leaked, f"子进程环境里出现了写库凭据：{leaked}"


def test_child_env_extra_overrides_whitelist(monkeypatch):
    """调用方 spec.env 可覆盖白名单同名项（如只给某个 server 换模型）。"""
    from src.core.config import settings
    from src.mcp_client import _child_env

    monkeypatch.setattr(settings, "llm_model", "from-settings")
    assert _child_env({"LLM_MODEL": "x-probe"}).get("LLM_MODEL") == "x-probe"
