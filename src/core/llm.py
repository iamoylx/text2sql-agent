"""
LLM 客户端工厂（P2 版，镜像 P1 同构设计）。

用途：全项目唯一 LLM 实例来源，通过 settings.llm_provider 切换 Agnes / DeepSeek。
原理：两家都实现 OpenAI 协议，统一用 langchain_openai.ChatOpenAI；temperature=0 保证
      SQL 生成确定性（SQL 生成场景温度非 0 会导致同样的问句两次生成不同 SQL）。
"""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from src.core.config import settings


def get_llm(*, streaming: bool = False) -> BaseChatModel:
    provider = settings.llm_provider
    if provider == "deepseek":
        return ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout,
            streaming=streaming,
        )
    # 默认 agnes。max_retries=1：SDK 内置指数退避在 429 时会拖 10-20 分钟
    # （S8 双跑实测踩坑），重试职责上移给调用层（_llm_json 20s 退避 / 评测层 60s 退避）
    return ChatOpenAI(
        model=settings.agnes_model,
        api_key=settings.agnes_api_key,
        base_url=settings.agnes_base_url,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_timeout,
        streaming=streaming,
        max_retries=1,
    )
