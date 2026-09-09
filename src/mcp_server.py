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
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP

from src.tools.registry import (
    _tool_execute_sql,
    _tool_generate_sql,
    _tool_compute_metric,
    _tool_render_chart,
)

mcp = FastMCP("p2-text2sql", instructions="Olist 电商数据的 Text2SQL 查询工具集（只读，四层安全）")


@mcp.tool()
def generate_sql(question: str, tables: list[str] | None = None) -> str:
    """把业务问题转成只读 SELECT SQL（SQLite 方言，内部自动注入 Schema 与 few-shot）。"""
    out = _tool_generate_sql({"question": question, "tables": tables or []})
    return json.dumps(out, ensure_ascii=False, default=str)


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


if __name__ == "__main__":
    if "--http" in sys.argv:
        port = int(sys.argv[sys.argv.index("--http") + 1]) if len(sys.argv) > sys.argv.index("--http") + 1 else 8600
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
