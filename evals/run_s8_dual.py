"""
P2-S8 双跑对照评测：同一套 8 条行为用例，单 Agent（agentic）vs Supervisor 多智能体。

对比维度（面试核心叙事）：
  正确率    —— 行为判定与 S3/S4 完全同口径（同 CASES 零漂移），Supervisor 不应退化
  LLM 调用数 —— Supervisor 用「规划+分工+判官」的额外调用换质量门控（诚实展示成本）
  token     —— AGNES 用量对比
  延迟      —— 端到端秒数

运行：.venv/Scripts/python.exe evals/run_s8_dual.py
产出：evals/s8_dual_result.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_s3_behavior import CASES                      # noqa: E402 同一套用例
from src.agent.agentic_graph import answer_agentic, build_agentic_graph   # noqa: E402
from src.agent.supervisor_graph import answer_supervisor, build_supervisor_graph  # noqa: E402


def judge_case(exp: str, status: str, msg: str, rows: list) -> bool:
    """与 run_s3_behavior / run_s4_behavior 逐字同口径的行为判定。"""
    if exp == "ok_result":
        return status == "ok" and len(rows) > 0
    if exp == "empty_ok":
        return status == "ok" and any(k in msg for k in
                                      ["0 笔", "不存在", "无记录", "暂无", "没有匹配"])
    if exp == "safe_no_write":
        return status in ("ok", "blocked") and len(msg) > 20
    if exp == "no_such_table":
        return (status == "blocked") or ("不存在" in msg or "没有" in msg
                                         or "无法" in msg or "未找到" in msg
                                         or "不支持" in msg or "不在" in msg)
    if exp == "ok_or_degraded":
        return status in ("ok", "degraded") and len(msg) > 20
    return status in ("ok", "blocked") and len(msg) > 20   # ok_clarified


_PROGRESS = ROOT / "evals" / "s8_progress.jsonl"


def _load_done(graph_type: str) -> dict[str, dict]:
    """断点续跑：读历史进度，已成功的用例跳过（S5 同款评测卫生）。"""
    done: dict[str, dict] = {}
    if _PROGRESS.exists():
        for line in _PROGRESS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("graph") == graph_type:
                done[r["id"]] = r
    return done


def run_one(graph_type: str, graph, answer_fn) -> list[dict]:
    done = _load_done(graph_type)
    results: list[dict] = []
    for c in CASES:
        if c["id"] in done:
            r = done[c["id"]]
            results.append(r)
            print(f"  [{graph_type}] SKIP {c['id']}（已有断点结果：{'PASS' if r['ok'] else 'FAIL'}）",
                  flush=True)
            continue
        t1 = time.time()
        # 429 免费额度限流：退避 60s 重试一次（限流恢复后继续，不算失败）
        for attempt in (1, 2):
            try:
                st = answer_fn(graph, c["q"], thread_id=f"s8-{graph_type}-{c['id']}")
                if "RateLimitError" in (st.get("message") or "") and attempt == 1:
                    print(f"  [{graph_type}] {c['id']} 命中 429，退避 60s 后重试…", flush=True)
                    time.sleep(60)
                    continue
                break
            except Exception as e:
                st = {"status": "degraded", "message": f"异常 {type(e).__name__}: {e}"}
                break
        msg = (st.get("message") or "")[:200]
        rows = st.get("result") or []
        status = st.get("status", "?")
        ok = judge_case(c["expect"], status, msg, rows)
        rec = {
            "id": c["id"], "q": c["q"], "expect": c["expect"],
            "ok": ok, "status": status, "message": msg,
            "rows": len(rows),
            "llm_calls": st.get("steps"),          # supervisor: 全图 LLM 调用数
            "tool_steps": None if graph_type == "supervisor" else st.get("steps"),
            "tokens": st.get("token_cost"),
            "latency_s": round(time.time() - t1, 1),
            "graph": graph_type,
        }
        results.append(rec)
        with open(_PROGRESS, "a", encoding="utf-8") as f:   # 每例落盘，429/中断可续跑
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  [{graph_type}] {'PASS' if ok else 'FAIL'} {c['id']} "
              f"status={status} calls={st.get('steps')} tok={st.get('token_cost')} "
              f"({time.time()-t1:.0f}s)", flush=True)
    return results


def summarize(results: list[dict]) -> dict:
    n = len(results)
    return {
        "pass_rate": round(sum(1 for r in results if r["ok"]) / n, 3),
        "total_llm_calls": sum(int(r.get("llm_calls") or 0) for r in results),
        "total_tokens": sum(int(r.get("tokens") or 0) for r in results),
        "total_latency_s": round(sum(r["latency_s"] for r in results), 1),
    }


def main() -> None:
    skip_agentic = "--skip-agentic" in sys.argv
    t0 = time.time()
    print("===== 双跑对照：单 Agent（agentic） vs Supervisor 多智能体 =====", flush=True)
    if skip_agentic:
        prev = json.loads((ROOT / "evals" / "s8_dual_result.json").read_text(encoding="utf-8"))
        r1 = prev["agentic"]["results"]      # 复用已有 agentic 结果（用例/口径零漂移）
        print("--- ① agentic：复用上一轮结果 ---", flush=True)
    else:
        print("--- ① agentic（S4 工具循环） ---", flush=True)
        g1, _ = build_agentic_graph()
        r1 = run_one("agentic", g1, answer_agentic)
    print("--- ② supervisor（S8 规划-分工-判官） ---")
    g2, _ = build_supervisor_graph()
    r2 = run_one("supervisor", g2, answer_supervisor)

    s1, s2 = summarize(r1), summarize(r2)
    print("\n===== 对照汇总 =====")
    print(f"{'指标':<14}{'agentic':>12}{'supervisor':>14}")
    print(f"{'正确率':<14}{s1['pass_rate']:>12}{s2['pass_rate']:>14}")
    print(f"{'LLM 调用数':<12}{s1['total_llm_calls']:>12}{s2['total_llm_calls']:>14}")
    print(f"{'token':<14}{s1['total_tokens']:>12}{s2['total_tokens']:>14}")
    print(f"{'延迟(秒)':<13}{s1['total_latency_s']:>12}{s2['total_latency_s']:>14}")

    (ROOT / "evals" / "s8_dual_result.json").write_text(json.dumps({
        "cases": len(r1),
        "agentic": {"summary": s1, "results": r1},
        "supervisor": {"summary": s2, "results": r2},
        "elapsed_s": round(time.time() - t0),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n已写入 evals/s8_dual_result.json")


if __name__ == "__main__":
    main()
