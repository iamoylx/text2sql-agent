"""P2-S6 API 启动入口（FastAPI + SSE，端口 8501）。

运行：python scripts/run_api.py
注意：LLM Key（AGNES_API_KEY / DEEPSEEK_API_KEY）从环境变量或项目根 .env 读取，
     缺失时服务能启动，但发起问答会报 OpenAIError: Missing credentials。
"""
from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

# 保证 uvicorn 能 import 到 src 包（直接 python scripts/run_api.py 时 sys.path[0]=scripts）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    uvicorn.run("src.api.server:app", host="127.0.0.1", port=8501, reload=False)
