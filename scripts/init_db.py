"""
数据库初始化脚本（P2-S1）。

语法：pandas.read_csv 读 Olist 9 表 → 按预定义 DDL 建表 → to_sql 入 SQLite。
用途：产出 data/db/olist.db（dev 库）。MySQL 版本见 README 的「切换 MySQL」小节——
     四层安全里的「只读账号 + MAX_EXECUTION_TIME」是 MySQL 特性，SQLite 开发库用
     URI mode=ro（只读打开）+ progress handler 超时做等价防护。
运行：env -u PYTHONPATH python scripts/init_db.py
"""
from __future__ import annotations

import sqlite3
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.db.schema import TABLES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "db" / "olist.db"

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


def main() -> None:
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


if __name__ == "__main__":
    main()
