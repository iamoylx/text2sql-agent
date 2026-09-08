"""
四层安全校验器的 pytest 套件（安全拦截必须 100%——P2-S5 验收标准之一）。

覆盖：
  - 语句类型白名单：DELETE/UPDATE/INSERT/DROP 拒绝，注释/字符串内关键字不误杀
  - 表名白名单：直接越权 / sqlite_master / 子查询绕过 / 逗号多表
  - 拼接注入：; DROP TABLE 拒绝
  - LIMIT 注入：无 LIMIT 自动补 LIMIT 1000；已有不重复
  - 只读：mode=ro 引擎层拒绝写
  - 超时：progress_handler 中断慢查询（百万行扫描）
运行：.venv/Scripts/python.exe -m pytest tests/test_safety.py -v
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from src.safety.validator import execute_with_timeout, validate_sql

DB = ROOT / "data" / "db" / "olist.db"

VALID = [
    ("SELECT COUNT(*) FROM orders"),
    ("SELECT c.customer_state, COUNT(*) FROM orders o JOIN customers c "
     "ON o.customer_id=c.customer_id WHERE o.order_status='delivered' GROUP BY 1"),
    ("SELECT * FROM orders /* DELETE FROM orders */"),          # 注释里藏关键字
    ("SELECT * FROM orders WHERE order_status='drop table'"),    # 字符串里含关键字
    ("select * from ORDERS"),                                   # 大小写
]


@pytest.mark.parametrize("sql", VALID)
def test_valid_select_passes(sql):
    r = validate_sql(sql)
    assert r.passed, r.reason
    assert r.sql.rstrip().endswith(";")


ATTACKS = [
    ("SELECT * FROM orders; DROP TABLE orders", "拼接注入"),
    ("DELETE FROM orders", "DELETE"),
    ("UPDATE orders SET order_status='x'", "UPDATE"),
    ("INSERT INTO orders VALUES ('x')", "INSERT"),
    ("SELECT * FROM (SELECT * FROM orders) t; DROP TABLE customers", "子查询后拼接"),
    ("SELECT * FROM users", "越权表"),
    ("SELECT name FROM sqlite_master", "系统表"),
    ("SELECT * FROM orders WHERE customer_id IN (SELECT customer_id FROM secret_table)", "子查询绕过"),
    ("SELECT * FROM orders, users", "逗号多表越权"),
    ("SELECT * FROM orders INTO OUTFILE /tmp/x", "数据外带"),
]


@pytest.mark.parametrize("sql,desc", ATTACKS)
def test_attack_blocked(sql, desc):
    r = validate_sql(sql)
    assert not r.passed, f"应拦截: {desc}"
    assert r.layer in ("stmt_type", "table"), r.layer


def test_limit_injected_once():
    r = validate_sql("SELECT * FROM orders")
    assert r.sql.count("LIMIT") == 1 and "LIMIT 1000" in r.sql
    r2 = validate_sql("SELECT * FROM orders LIMIT 5")
    assert r2.sql.count("LIMIT") == 1  # 已有 LIMIT 不重复注入


def test_readonly_blocks_write():
    ok, err, _ = execute_with_timeout(str(DB), "DELETE FROM orders")
    assert not ok and "readonly" in err.lower()


def test_timeout_interrupts_slow_query():
    ok, err, _ = execute_with_timeout(
        str(DB), "SELECT * FROM geolocation ORDER BY geolocation_lat DESC", timeout_ms=200,
    )
    assert not ok and "TimeoutError" in err and "200ms" in err


def test_normal_query_with_timeout_ok():
    ok, err, rows = execute_with_timeout(str(DB), "SELECT COUNT(*) AS c FROM orders")
    assert ok and rows[0]["c"] == 99441


# ---------------- S8 补：CTE 别名不得被表名白名单误杀 ----------------

class TestCteWhitelist:
    SQL_CTE = (
        'WITH max_month AS ('
        '  SELECT strftime("%Y-%m", order_purchase_timestamp) AS m, COUNT(*) AS n'
        '  FROM orders WHERE order_status=\'delivered\' GROUP BY m)'
        ' SELECT m, n FROM max_month ORDER BY n DESC LIMIT 1'
    )

    def test_cte_alias_not_treated_as_table(self):
        from src.safety.validator import validate_sql
        r = validate_sql(self.SQL_CTE)
        assert r.passed, f"CTE 别名被误杀: {r.reason}"

    def test_cte_does_not_mask_real_violation(self):
        from src.safety.validator import validate_sql
        sql = ('WITH t AS (SELECT * FROM orders)'
               ' SELECT * FROM t JOIN hackers ON 1=1')
        r = validate_sql(sql)
        assert not r.passed and 'hackers' in r.reason

    def test_cte_names_extraction(self):
        from src.safety.validator import extract_cte_names
        assert extract_cte_names(self.SQL_CTE) == {"max_month"}
        assert extract_cte_names("SELECT 1") == set()

    def test_cte_in_writer_path(self):
        # 写路径同样排除 CTE（理论上少用，但口径必须一致）
        from src.safety.writer import validate_write
        sql = ('WITH target AS (SELECT order_id FROM orders WHERE order_id=\'o1\')'
               ' UPDATE orders SET status=\'x\' WHERE order_id IN (SELECT order_id FROM target)')
        r = validate_write(sql)
        assert r.passed, f"CTE 写法被误杀: {r.reason}"
