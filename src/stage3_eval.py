"""阶段 3：评估 Text-to-SQL Agent。

指标（沿用学术基准的做法）：
    执行准确率（Execution Accuracy）
        = 生成的 SQL 执行结果 == 标准 SQL 执行结果 的比例
    有效 SQL 率
        = 生成的 SQL 能跑通（不报语法/字段错）的比例
    平均步数 / 平均工具调用次数
        = 效率指标

"""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))

from questions import QUESTIONS          # noqa: E402
from stage4_agent import DB, run_agent, load_env, set_variant   # noqa: E402

import os
import sqlite3


# ------------------------------------------------------------ 结果比对

def normalize(rows) -> list:
    """把结果集标准化成可比较的形式。"""
    out = []
    for row in rows:
        out.append(tuple(
            round(float(v), 4) if isinstance(v, (int, float)) else str(v)
            for v in row
        ))
    return sorted(out, key=repr)

def flat_values(rows) -> set:
    """把结果集摊平成「值」的集合，用于宽松比对。"""
    out = set()
    for row in rows:
        for v in row:
            out.add(round(float(v), 4) if isinstance(v, (int, float)) else str(v))
    return out


def contains_gold(gold_rows, agent_rows) -> bool:
    """宽松判定：gold 里的每一个值，都能在 agent 的结果里找到。

    """
    agent_vals = flat_values(agent_rows)
    gold_vals = flat_values(gold_rows)
    return gold_vals.issubset(agent_vals)

def exec_sql(sql: str):
    """执行一条 SQL，返回 (rows, error)。"""
    conn = sqlite3.connect(DB)
    try:
        return conn.execute(sql).fetchall(), None
    except Exception as exc:                     # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


REFUSAL_WORDS = ("无法", "不能", "没有", "不存在", "未找到", "缺少", "不包含", "未提供")


def is_refusal(answer: str) -> bool:
    """粗略判断 Agent 是不是在拒答。"""
    return any(w in answer for w in REFUSAL_WORDS)


# ------------------------------------------------------------ 单题评测

def evaluate_one(item, api_key: str, verbose: bool = False):
    """跑一道题，返回结果字典。"""
    result = {
        "id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "answer": None,
        "steps": 0,
        "tool_calls": 0,
        "valid_sql": False,
        "correct_strict": False,
        "correct": False,
        "note": "",
    }

    try:
        answer, trace = run_agent(item["question"], api_key, verbose=verbose)
    except Exception as exc:                      # noqa: BLE001
        result["note"] = f"Agent 崩溃: {type(exc).__name__}: {exc}"
        return result

    result["answer"] = answer
    result["steps"] = len(trace)
    result["tool_calls"] = sum(1 for t in trace if t["type"] == "tool")
    if "达到最大步数" in answer or "未能得出结论" in answer:
        result["correct"] = False
        result["correct_strict"] = False
        result["note"] = "⚠️ 达到最大步数，未得出结论"
        return result

    # ---- 不可答类：看它有没有拒答 ----
    if item["gold_sql"] is None:
        result["correct"] = is_refusal(answer)
        result["note"] = "拒答" if result["correct"] else "⚠️ 没拒答（可能硬编了）"
        return result

    # ---- 可答类：比对 SQL 执行结果 ----
    gold_rows, gold_err = exec_sql(item["gold_sql"])
    if gold_err:
        result["note"] = f"❌ 标准 SQL 本身有错: {gold_err}"
        return result

    # ---- 收集 trace 里【所有】成功执行的 SQL 结果 ----
    # ★ 为什么不是只取最后一次：
    #   Agent 可能先算出答案、然后又查了一次明细来"展示过程"。
    #   实测 q007 就是这样 —— 它先算了平均值（正确），
    #   再查明细（用来在回答里列出来），只看最后一次就会把"答对"判成"答错"。
    all_rows = []
    for t in trace:
        if t["type"] != "tool" or t["tool"] != "run_sql":
            continue
        rows, err = exec_sql(t["arg"])
        if err is None:
            result["valid_sql"] = True
            all_rows.append(rows)

    if not all_rows:
        result["note"] = "Agent 没有产出可执行的 SQL"
        return result

    last_rows = all_rows[-1]

    # 严格：trace 里有【任意一次】查询的结果集与标准 SQL 完全一致
    strict = any(normalize(r) == normalize(gold_rows) for r in all_rows)
    # 宽松：trace 里有【任意一次】查询的结果包含标准答案的值
    loose = any(contains_gold(gold_rows, r) for r in all_rows)

    result["correct_strict"] = strict
    result["correct"] = loose              # 宽松指标作为主指标
    if not loose:
        result["note"] = f"结果不符: 期望 {gold_rows[:2]} / 实际 {last_rows[:2]}"
    elif not strict:
        result["note"] = "（宽松算对：多查了列）"
    return result

# ------------------------------------------------------------ 主流程
def main():
    load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise SystemExit("找不到 DEEPSEEK_API_KEY，请在 .env 里配置")

    # ---- 解析命令行参数 ----
    #   python src/stage3_eval.py                   跑全部
    #   python src/stage3_eval.py unit              只跑某一类型
    #   python src/stage3_eval.py q010              只跑某一题
    #   python src/stage3_eval.py --repeat 3        重复 3 次
    #   python src/stage3_eval.py unit --repeat 3   组合使用
    # ★ 关键顺序：先把所有「带值的选项」从参数里【取出来并删掉】，
    #   最后剩下不以 -- 开头的才是筛选条件。
    #   否则 --variant 的值（如 A_schema）会被当成"只跑这个类型"。
    args = list(sys.argv[1:])

    def pop_option(name: str):
        """取出 --name VALUE 并把它俩一起从 args 删掉；不存在则返回 None。

        用「取值即删除」而不是「先读一遍再过滤」，是为了从根上避免
        『选项的值泄漏成筛选条件』这类 bug。
        """
        if name not in args:
            return None
        k = args.index(name)
        if k + 1 >= len(args):
            raise SystemExit(f"{name} 后面要跟一个值")
        value = args[k + 1]
        del args[k:k + 2]
        return value

    repeat_raw = pop_option("--repeat")
    repeat = 1
    if repeat_raw is not None:
        try:
            repeat = int(repeat_raw)
        except ValueError:
            raise SystemExit("--repeat 后面要跟一个整数，例如 --repeat 3")

    variant = pop_option("--variant") or "baseline"
    set_variant(variant)

    # 剩下的第一个非选项参数 = 筛选条件（类型名或题号）
    rest = [a for a in args if not a.startswith("--")]
    only = rest[0] if rest else None

    items = [q for q in QUESTIONS
             if only is None or q["category"] == only or q["id"] == only]

    if not items:
        raise SystemExit(
            f"没有匹配的题目（筛选条件 = {only!r}）。\n"
            f"  可用类型：{sorted({q['category'] for q in QUESTIONS})}\n"
            f"  可用题号：{[q['id'] for q in QUESTIONS]}"
        )

    print(f"评估集：{len(items)} 题 | 重复 {repeat} 次 | 变体 {variant}"
          + (f" | 筛选 {only}" if only else ""))

    all_runs = []
    for run in range(1, repeat + 1):
        if repeat > 1:
            print("\n" + "#" * 74)
            print(f"# 第 {run}/{repeat} 次运行")
            print("#" * 74)

        results = []
        for i, item in enumerate(items, 1):
            print(f"[{i:>2}/{len(items)}] {item['id']} {item['question']}")
            r = evaluate_one(item, api_key)
            mark = "✅" if r["correct"] else "❌"
            print(f"        {mark}  {r['steps']} 步 / {r['tool_calls']} 次工具  {r['note']}")
            if r.get("answer"):
                flat = " ".join(r["answer"].split())
                print(f"        ⤷ 回答: {flat[:110]}{'…' if len(flat) > 110 else ''}")
            results.append(r)

        n = len(results)
        ans = [r for r in results if r["category"] != "unanswerable"]
        unans = [r for r in results if r["category"] == "unanswerable"]

        all_runs.append({
            "strict": (sum(r["correct_strict"] for r in ans) / len(ans)) if ans else None,
            "loose": (sum(r["correct"] for r in ans) / len(ans)) if ans else None,
            "valid": (sum(r["valid_sql"] for r in ans) / len(ans)) if ans else None,
            "refusal": (sum(r["correct"] for r in unans) / len(unans)) if unans else None,
            "steps": sum(r["steps"] for r in results) / n,
            "tools": sum(r["tool_calls"] for r in results) / n,
            "results": results,
        })

    # ---- 汇总（多次运行取平均 ± 范围）----
    def agg(key):
        vals = [run[key] for run in all_runs if run[key] is not None]
        if not vals:
            return None, None, None
        return sum(vals) / len(vals), min(vals), max(vals)

    print("\n" + "=" * 74)
    print(f"总体（{repeat} 次运行取平均）")
    print("-" * 74)
    for key, label in [("strict", "严格执行准确率"), ("loose", "宽松执行准确率"),
                       ("valid", "有效 SQL 率"), ("refusal", "拒答准确率"),
                       ("steps", "平均步数"), ("tools", "平均工具调用")]:
        mean, lo, hi = agg(key)
        if mean is None:
            print(f"  {label:<16} {'—':>7}   （本次没有这类题目）")
        elif key in ("steps", "tools"):
            print(f"  {label:<16} {mean:>7.2f}   (范围 {lo:.2f} ~ {hi:.2f})")
        else:
            print(f"  {label:<16} {mean:>7.4f}   (范围 {lo:.4f} ~ {hi:.4f})")

    # ---- 按类型（用最后一次运行）----
    last = all_runs[-1]["results"]
    print("\n按问题类型（最后一次运行）")
    print("-" * 74)
    for c in sorted({r["category"] for r in last}):
        sub = [r for r in last if r["category"] == c]
        a = sum(r["correct"] for r in sub) / len(sub)
        print(f"  {c:<14} {a:.4f}  ({sum(r['correct'] for r in sub)}/{len(sub)})")

    # ---- 落盘 ----
    out = Path("outputs")
    out.mkdir(exist_ok=True)
    refusal = agg("refusal")[0]
    (out / "eval_results.json").write_text(
        json.dumps({
            "summary": {
                "repeat": repeat,
                "strict_accuracy": round(agg("strict")[0] or 0.0, 4),
                "loose_accuracy": round(agg("loose")[0] or 0.0, 4),
                "valid_sql_rate": round(agg("valid")[0] or 0.0, 4),
                "refusal_accuracy": round(refusal, 4) if refusal is not None else None,
                "avg_steps": round(agg("steps")[0], 2),
                "avg_tool_calls": round(agg("tools")[0], 2),
            },
            "by_category": {
                c: round(
                    sum(r["correct"] for r in last if r["category"] == c)
                    / max(1, sum(1 for r in last if r["category"] == c)), 4)
                for c in sorted({r["category"] for r in last})
            },
            "results": last,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n详细结果已写入 outputs/eval_results.json")

    print("\n" + "=" * 74)
    wrong = [r for r in last if not r["correct"]]
    if wrong:
        print("答错的题：")
        for r in wrong:
            print(f"  ❌ {r['id']} [{r['category']}] {r['question']}")
            print(f"       {r['note']}")
    else:
        print("全部答对 🎉")
if __name__ == "__main__":
    main()