"""
MCP Client（P2-S7）：动态发现外部 MCP server 的工具并注册进 agent 工具集。

架构（面试叙事）：
  - 「工具即插即用」：agent 的工具集不再写死——启动时连上 MCP server，list_tools
    发现能力，转成 OpenAI function-calling schema 注册进 registry，下一轮
    bind_tools 模型就能看见并调用。接一个新数据工具 = 改一行配置，不改图。
  - **外部工具不旁路安全**：注册走 registry.register_external_tool——参数先过
    Pydantic（由 MCP inputSchema 动态生成）再转发；同名原生工具优先不覆盖。
  - 同步桥：agent 图的 tools 节点是同步 dispatch，而 MCP client 是 async——
    用后台线程跑事件循环，sync 调用经 run_coroutine_threadsafe 桥接，
    会话（stdio 子进程 / http 连接）常驻不反复建连。

用法：
    from src.mcp_client import enable_mcp_tools
    registered = enable_mcp_tools([
        {"name": "p2", "transport": "stdio", "command": sys.executable,
         "args": ["src/mcp_server.py"]},
        {"name": "remote", "transport": "http", "url": "http://127.0.0.1:8600/mcp"},
    ])
    # registered = 发现并注册的工具名列表；此后 dispatch() 可直接调它们
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from src.tools.registry import register_external_tool

# MCP stdio 子进程的环境**不是**父进程 os.environ 的复制：SDK 只透传
# get_default_environment() 的白名单（HOME/PATH/TERM… 本机实测 12 个变量）。
# 于是「父进程明明有 DEEPSEEK_API_KEY，子进程却报 Missing credentials」——
# 根因在传参层，不在密钥配置层。
#
# 修法刻意**不做 `**os.environ` 全量透传**：那会把父进程里所有密钥（数据库口令、
# 云厂商 token、第三方 API key）一并塞进一个我们并未审计过全部代码的子进程，
# 等于用一个「修好了」换一个「泄露面无限大」。正确姿势是显式白名单——
# 子进程要自建 LLM 与只读连库，就只给它这两类凭据。
_MCP_ENV_PASSTHROUGH = (
    # LLM 凭据与选型（子进程 mcp_server.py 复用同一套 get_llm）
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_THINKING",
    "AGNES_API_KEY", "AGNES_BASE_URL",
    "LLM_PROVIDER", "LLM_MODEL",
    # 数据源与场景（只读连接串；写口令 agent_rw 刻意**不在**白名单内）
    "DB_URL", "DB_RO_URI", "SCENARIO",
)


def _child_env(extra: dict | None = None) -> dict[str, str]:
    """构造 stdio 子进程环境：SDK 安全基线 + 显式白名单 + 调用方覆盖。"""
    from mcp.client.stdio import get_default_environment

    env = dict(get_default_environment())
    for key in _MCP_ENV_PASSTHROUGH:
        value = os.environ.get(key)
        if value:
            env[key] = value
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def _mcp_to_openai_schema(tool) -> dict:
    """MCP Tool(name/description/inputSchema) → OpenAI function-calling schema。"""
    input_schema = tool.inputSchema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "").strip() or f"MCP tool: {tool.name}",
            "parameters": input_schema,
        },
    }


class MCPToolBridge:
    """MCP 会话桥：后台线程事件循环 + 常驻会话 + 同步调用接口。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, Any] = {}
        self._stacks: dict[str, Any] = {}     # AsyncExitStack：持住常驻上下文防 GC 断连
        self._lock = threading.Lock()

    # ---- 生命周期 ----
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever,
                                 daemon=True, name="mcp-bridge").start()
            return self._loop

    def _submit(self, coro, timeout: float = 60):
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result(timeout)

    # ---- 连接（stdio / http） ----
    async def _connect_stdio(self, name: str, command: str, args: list[str],
                             env: dict | None = None) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # 必须显式给 env：SDK 在 server.env 为 None 时只透传安全白名单，
        # 子进程读不到 LLM 凭据（见 _MCP_ENV_PASSTHROUGH 上方注释）
        params = StdioServerParameters(command=command, args=args,
                                       env=_child_env(env))
        # 常驻连接：AsyncExitStack 持住全部上下文——若不持有 CM 对象引用，
        # GC 回收任务组会直接把连接关掉（实测踩坑：连接秒断 "Connection closed"）
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = ClientSession(read, write)
        await stack.enter_async_context(session)
        await session.initialize()
        self._sessions[name] = session
        self._stacks[name] = stack

    async def _connect_http(self, name: str, url: str) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        stack = AsyncExitStack()
        read, write, _get_session_id = await stack.enter_async_context(
            streamablehttp_client(url))
        session = ClientSession(read, write)
        await stack.enter_async_context(session)
        await session.initialize()
        self._sessions[name] = session
        self._stacks[name] = stack

    def connect_stdio(self, name: str, command: str, args: list[str],
                      env: dict | None = None) -> None:
        self._submit(self._connect_stdio(name, command, args, env), timeout=60)

    def connect_http(self, name: str, url: str) -> None:
        self._submit(self._connect_http(name, url), timeout=60)

    # ---- 发现与调用 ----
    def list_tools(self, name: str) -> list:
        async def _go():
            return (await self._sessions[name].list_tools()).tools
        return self._submit(_go(), timeout=30)

    def call(self, name: str, tool: str, args: dict, timeout: float = 60) -> dict:
        """调远端工具：MCP CallToolResult → 统一 {ok, ...} dict（与原生工具同形）。"""
        async def _go():
            return await self._sessions[name].call_tool(tool, args)

        res = self._submit(_go(), timeout=timeout)
        text = res.content[0].text if res.content else "{}"
        try:
            out = json.loads(text)
        except Exception:
            # 非 JSON 文本响应是合法的 MCP 结果（如纯文本工具）——只有 isError 才算失败
            if getattr(res, "isError", False):
                out = {"ok": False, "error": text[:200]}
            else:
                out = {"ok": True, "text": text}
        if getattr(res, "isError", False):
            out.setdefault("ok", False)
        return out


_BRIDGE: MCPToolBridge | None = None


def get_bridge() -> MCPToolBridge:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = MCPToolBridge()
    return _BRIDGE


def enable_mcp_tools(server_specs: list[dict]) -> list[str]:
    """连接 MCP server → 发现工具 → 注册进 registry。

    server_specs 元素：
      {"name": "别名", "transport": "stdio", "command": "...", "args": [...],
       "env": {...}}                        # env 可选，覆盖白名单里的同名项
      {"name": "别名", "transport": "http", "url": "http://host:port/mcp"}

    返回实际注册的工具名列表（与原生四工具同名的会被跳过——原生优先）。
    """
    bridge = get_bridge()
    registered: list[str] = []
    for spec in server_specs:
        name = spec["name"]
        if spec["transport"] == "stdio":
            bridge.connect_stdio(name, spec["command"], spec.get("args") or [],
                                 spec.get("env"))
        elif spec["transport"] == "http":
            bridge.connect_http(name, spec["url"])
        else:
            raise ValueError(f"未知 MCP transport: {spec['transport']}")
        for tool in bridge.list_tools(name):
            disp = (lambda sn, tn: lambda args: bridge.call(sn, tn, args))(name, tool.name)
            reg = register_external_tool(_mcp_to_openai_schema(tool),
                                         tool.inputSchema or {}, disp)
            if reg:
                registered.append(reg)
    return registered
