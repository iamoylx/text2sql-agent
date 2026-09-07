"""
数据库连接模块。

用途：所有 SQL 执行都走这里拿连接，禁止业务代码直接 sqlite3.connect。
原理：
  - db_url / db_ro_uri 双通道：开发期 SQLite（读=mode=ro 只读 URI，写=普通连接）；
    生产切 MySQL 时 db_url 换 mysql+pymysql://agent_ro:xxx@host/olist 即可，代码零改动
    ——「SQLAlchemy URL 抽象」正是简历「只读账号」这一层在 SQLite 开发期的等价实现。
  - mode=ro 是 SQLite 层面的只读兜底：即使 SQL 注入绕过了解析层，数据库引擎也会拒绝
    INSERT/UPDATE/DELETE（file:...?mode=ro 打开时任何写操作直接抛 ReadOnlyDatabase）。
"""
from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from src.core.config import settings


def get_conn(*, readonly: bool = True) -> Connection:
    """返回一个数据库连接。默认只读（SQLite mode=ro / MySQL agent_ro 账号）。"""
    if readonly:
        # SQLite 只读 URI：mode=ro 由引擎层面拒绝一切写操作
        engine = create_engine(
            f"sqlite:///{settings.db_ro_uri}",
            connect_args={"uri": True},
            future=True,
        )
    else:
        engine = create_engine(settings.db_url, future=True)
    return engine.connect()


def execute_select(sql: str, *, limit: int | None = 1000) -> list[dict]:
    """只读执行一条 SELECT，返回 list[dict]（列名 -> 值）。"""
    conn = get_conn(readonly=True)
    try:
        rows = conn.execute(text(sql)).mappings().all()
        if limit is not None and len(rows) > limit:
            rows = rows[:limit]
        return [dict(r) for r in rows]
    finally:
        conn.close()
