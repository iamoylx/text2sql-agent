"""
P2 四工具的 MCP Server（P2-S7）：把 Text2SQL 能力以标准 MCP 协议暴露给任何第三方客户端。

设计立场（面试叙事）：
  - 复用 > 重写：四个工具直接调 src/tools/registry 的同一实现（含四层安全），
    MCP 只是「协议外壳」——通过 MCP 进来的 execute_readonly_sql 依然走
    只读账号 + 表白名单 + LIMIT + 超时，**不因换协议而旁路安全层**。
  - 两种传输：stdio（Claude Desktop / Cursor 等桌面客户端标配）+
    streamable-http（远程/服务间调用，本仓库自建 client 用）。
  - data_ref 适配：registry 的 compute_metric/render_chart 依赖进程内 results_registry
    （data_ref="result" 是图内概念）；MCP 是无状态协议，改为调用方直接传 rows。

启动：
  stdio:   python src/mcp_server.py                 （子进程被客户端拉起，stdin/stdout 通信）
  http:    python src/mcp_server.py --http 8600     （streamable-http，端点 http://127.0.0.1:8600/mcp）

启动期体检（P2 加固，2026-10-18）：
  `mcp.run()` 之前先 `require_llm_credentials()`——缺凭据是**部署错误**，必须在启动时暴露，
  而不是等客户端第一次调 generate_sql 才炸（那时现象是「工具好像坏了」，很难归因）。
    · 默认（硬失败）：stderr 打「缺哪个变量 + 怎么修」→ `sys.exit(1)`
    · `--allow-degraded`：stderr 打 WARNING → **不注册 generate_sql**，其余 3 个工具照常
      （compute/execute/render 都不需要 LLM）。降级是显式的，且留了声音。
  ★ stdout 纪律：stdio 模式下 stdout 是**协议信道**，一个字节都不能脏。所有诊断/日志一律
    走 stderr。`tests/test_mcp.py` 有一条用例逐行断言 stdout 全是合法 JSON-RPC 帧。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP

# FastMCP 自己的 Settings 里有个未解析的前向引用（`lifespan`），pydantic-settings 每次
# 起服务都会往 stderr 吐一条 IncompleteFieldDefinitionWarning。它不影响本项目任何行为
# （我们不用 lifespan），但会把「缺凭据」这类**部署报错**挤到警告后面，部署脚本 grep 时
# 第一眼看到的是噪声。只过滤这一个类别，其他 pydantic_settings 警告照常输出。
try:                                                    # pragma: no cover
    import warnings

    from pydantic_settings.sources.utils import IncompleteFieldDefinitionWarning

    warnings.filterwarnings("ignore", category=IncompleteFieldDefinitionWarning)
except Exception:
    pass

from src.core.llm import MissingCredentialError, require_llm_credentials
from src.tools.registry import (
    _tool_execute_sql,
    _tool_generate_sql,
    _tool_compute_metric,
    _tool_render_chart,
)

mcp = FastMCP("p2-text2sql", instructions="Olist 电商数据的 Text2SQL 查询工具集（只读，四层安全）")


@mcp.tool()
def execute_readonly_sql(sql: str) -> str:
    """四层安全校验（只读/表白名单/LIMIT/超时）后执行 SELECT，返回结果行。"""
    out = _tool_execute_sql({"sql": sql})
    return json.dumps(out, ensure_ascii=False, default=str)


@mcp.tool()
def compute_metric(metric: str, rows: list[dict], value_col: str,
                   group_by: list[str] | None = None) -> str:
    """对结果行做二次指标：sum/avg/count/max/min/mom/yoy/ratio（rows 为 execute 返回的结果行）。"""
    out = _tool_compute_metric(
        {"metric": metric, "data_ref": "result", "value_col": value_col,
         "group_by": group_by or []},
        {"result": rows})
    return json.dumps(out, ensure_ascii=False, default=str)


@mcp.tool()
def render_chart(chart_type: str, rows: list[dict], x: str, y: str) -> str:
    """生成 ECharts 图表配置（bar/line/pie）。rows 为结果行，x/y 为列名。"""
    out = _tool_render_chart(
        {"chart_type": chart_type, "x": x, "y": y, "data_ref": "result"},
        {"result": rows})
    return json.dumps(out, ensure_ascii=False, default=str)


# generate_sql 依赖 LLM 凭据，**是否暴露由启动期体检决定**，所以不挂 @mcp.tool() 装饰器，
# 而是在 __main__ 里按体检结果显式注册（缺凭据时它根本不出现在 tools/list 里）。
def generate_sql(question: str, tables: list[str] | None = None) -> str:
    """把业务问题转成只读 SELECT SQL（SQLite 方言，内部自动注入 Schema 与 few-shot）。"""
    out = _tool_generate_sql({"question": question, "tables": tables or []})
    return json.dumps(out, ensure_ascii=False, default=str)


def _startup_credential_check(argv: list[str]) -> bool:
    """启动期体检：返回 True=暴露 generate_sql；False=显式降级（不暴露）。

    诊断一律走 stderr：stdio 模式下 stdout 是协议信道，脏一个字节客户端就断连。
    """
    allow_degraded = "--allow-degraded" in argv
    try:
        require_llm_credentials()
        return True
    except MissingCredentialError as exc:
        if allow_degraded:
            first_line = str(exc).splitlines()[0].strip()
            print(f"[WARN] --allow-degraded：generate_sql 未暴露（{first_line}）", file=sys.stderr)
            return False
        # 硬失败：点名变量 + 修法（三步），不要包一层只剩「启动失败」的模糊话
        print(str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _expose_generate_sql = _startup_credential_check(sys.argv)
    if _expose_generate_sql:
        mcp.tool()(generate_sql)        # 先注册再 run，否则 tools/list 里看不到
    if "--http" in sys.argv:
        port = int(sys.argv[sys.argv.index("--http") + 1]) if len(sys.argv) > sys.argv.index("--http") + 1 else 8600
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
