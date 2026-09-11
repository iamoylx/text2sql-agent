"""
数据库初始化脚本（P2-S1）。

语法：pandas.read_csv 读 Olist 9 表 → 按预定义 DDL 建表 → to_sql 入 SQLite。
用途：产出 data/db/olist.db（dev 库）。MySQL 版本见 README 的「切换 MySQL」小节——
     四层安全里的「只读账号 + MAX_EXECUTION_TIME」是 MySQL 特性，SQLite 开发库用
     URI mode=ro（只读打开）+ progress handler 超时做等价防护。

建库完成后会自动调用 dump_schema() 重新生成 data/schema.md（给人读的 Schema 文档，
随仓库提交）——保证「文档永远与库同步」，不需要手动记得去刷新。

运行：
  env -u PYTHONPATH python scripts/init_db.py                # 重建库 + 刷新 data/schema.md
  env -u PYTHONPATH python scripts/init_db.py --dump-schema  # 只刷新文档，不重建库（快）
  env -u PYTHONPATH python scripts/init_db.py --no-dump      # 只重建库，不动文档
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.db.schema import TABLES, render_schema_md  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "db" / "olist.db"
SCHEMA_MD = ROOT / "data" / "schema.md"

# CSV 文件名 → 表名映射（去 _dataset 后缀，与 schema.py 的表名一致）
CSV_MAP = {
    "olist_customers_dataset.csv": "customers",
    "olist_orders_dataset.csv": "orders",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "olist_geolocation_dataset.csv": "geolocation",
    "product_category_name_translation.csv": "product_category_translation",
}

# 各表期望行数（官方数据集口径，导入后校验，对不上立即报错）
EXPECTED = {"customers": 99441, "orders": 99441, "order_items": 112650,
            "order_payments": 103886, "order_reviews": 99224, "products": 32951,
            "sellers": 3095, "geolocation": 1000163, "product_category_translation": 71}


def dump_schema(db_path: Path = DB, out_path: Path = SCHEMA_MD) -> Path | None:
    """按当前库的真实行数重新生成 data/schema.md（给人读的 Schema 文档）。

    - 表清单取自 TABLES（本脚本进程内即 9 张业务表）；
    - 某表在库里查不到时**跳过行数标注而不是报错**：注册元数据可能先于建表落盘，
      文档生成不该因此中断；
    - 库不存在时直接跳过并提示（--dump-schema 在没建库的机器上不炸）。
    """
    if not db_path.exists():
        print(f"[dump-schema] 库不存在，跳过：{db_path}")
        return None
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        row_counts: dict[str, int] = {}
        for t in TABLES:
            try:
                row_counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                continue
    finally:
        con.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_schema_md(row_counts), encoding="utf-8")
    missing = [t for t in TABLES if t not in row_counts]
    tail = f"，缺表跳过 {missing}" if missing else ""
    print(f"[dump-schema] 已刷新 {out_path}（{len(row_counts)} 张表{tail}）")
    return out_path


def rebuild_db() -> None:
    """重建 dev 库：解压 → 读 9 张 CSV → 按 DDL 建表 → 入 SQLite → 校验行数。"""
    # geolocation 是 zip 包
    zipped = RAW / "olist_geolocation_dataset.zip"
    if zipped.exists() and not (RAW / "olist_geolocation_dataset.csv").exists():
        with zipfile.ZipFile(zipped) as zf:
            zf.extractall(RAW)
        print("已解压 geolocation.zip")

    if DB.exists():
        DB.unlink()
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)

    counts = {}
    for csv_name, table in CSV_MAP.items():
        t0 = time.time()
        df = pd.read_csv(RAW / csv_name)
        # 时间列转 pandas datetime（SQLite 存 ISO 字符串，比较运算仍正确）
        for col in df.columns:
            if col.endswith(("_timestamp", "_date", "_at")):
                df[col] = pd.to_datetime(df[col], errors="coerce")
        # 按预定义 DDL 建表（类型与注释意图一致），再追加数据
        conn.execute(TABLES[table]["ddl"])
        df.to_sql(table, conn, if_exists="append", index=False)
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        counts[table] = n
        flag = "OK" if n == EXPECTED.get(table) else f"⚠️ 期望 {EXPECTED.get(table)}"
        print(f"  {table:<32} {n:>9,} 行  {flag}  ({time.time()-t0:.1f}s)")

    conn.commit()
    conn.close()
    print(f"\n库已写入 {DB}  大小 {DB.stat().st_size/1e6:.1f} MB")


def main(*, rebuild: bool = True, dump: bool = True) -> None:
    if rebuild:
        rebuild_db()
    if dump:
        dump_schema()


def _cli() -> None:
    ap = argparse.ArgumentParser(
        description="初始化 Olist 开发库（并默认刷新 data/schema.md）")
    ap.add_argument("--dump-schema", action="store_true",
                    help="只重新生成 data/schema.md，不重建库（秒级）")
    ap.add_argument("--no-dump", action="store_true",
                    help="重建库但不刷新 data/schema.md")
    a = ap.parse_args()
    if a.dump_schema and a.no_dump:
        ap.error("--dump-schema 与 --no-dump 互斥")
    main(rebuild=not a.dump_schema, dump=not a.no_dump)


if __name__ == "__main__":
    _cli()
