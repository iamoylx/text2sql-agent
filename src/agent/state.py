"""
SQL Agent 状态定义（P2-S3）。

与 P1 的 RAGState 同构设计：TypedDict + add_messages 注解字段。
关键字段设计理由（面试可讲）：
  - messages 用 add_messages：LangGraph 的 reducer，跨节点累积对话（含每轮工具调用的
    思考/观察，ReAct 循环的可视化轨迹来源）。
  - clarified_question：understand 节点消歧后的口径（与 P1 路由层 standalone 同思路——
    把「用户怎么说」归一成「任务怎么做」）。
  - retry_count：错误回喂自愈的重试计数，配 MAX_RETRY 硬上限防死循环烧 Token。
  - token_cost：累计 token，超阈值停止重试（简历「Token 成本上限」落地）。
  - result / analysis / chart_config：execute → analyze → respond 三节点的产出。
"""
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages


class SQLAgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]     # 对话历史（含思考/工具轨迹）
    question: str                               # 用户原始问题
    clarified_question: str                     # understand 消歧后的口径
    sql: str                                    # 当前生成的 SQL
    sql_error: str | None                       # 最近一次执行错误（自愈输入）
    retry_count: int                            # 自愈重试计数
    result: list[dict]                          # 查询结果 rows
    analysis: str                               # 指标解读
    chart_config: dict | None                   # ECharts 配置（S6 用）
    token_cost: int                             # 累计 token
    steps: int                                  # agentic 图：已完成 agent 轮数（步数上限防死循环）
    # ⚠️ 下面三行的 "= 默认值" 仅是文档性书写：TypedDict 运行时不生效（不会自动补默认），
    # 实际读取必须统一 state.get(key, 默认) —— 节点代码里正是这么写的。
    status: Literal["ok", "blocked", "degraded"] = "ok"
    message: str = ""                           # 给用户的最终回复
    reject_reason: str = ""                     # 安全拦截原因（留证）
