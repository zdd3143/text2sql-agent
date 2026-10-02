"""阶段 5.3：专门追踪「跳过查询、凭空编造」这个失败模式。

现象
----
在 A_schema 变体下，q011「中国石化和中国石油2023年营业收入相差多少」
有时会【一次 SQL 都不执行】就直接给出答案，而且数字看起来很精确：

    它说：32122.15 / 30110.12 / 2012.03
    真值：32122.2  / 30110.1  / 2012.1

三个数字全错，但内部自洽（32122.15 - 30110.12 = 2012.03）。
说明它是凭「世界知识」生成的，根本没查数据。

⚠️ 这比「查错了」危险得多：
   一个不查数据库、却能给出精确数字的 Text-to-SQL 系统，是最糟的情况。
   而且它看起来完全可信（精确到两位小数、内部自洽）。

这个脚本要回答一个问题
--------------------
    「跳过查询」的比例是多少？和 schema 规模 / 变体有关系吗？

用法
----
    # 在 3 张表的小库上测
    $env:T2S_DB="data/demo.db"
    python src/stage5_probe_fabrication.py

    # 在 292 张表的大库上测
    $env:T2S_DB="data/big.db"
    python src/stage5_probe_fabrication.py

    # 换题 / 改次数
    python src/stage5_probe_fabrication.py q007 8
"""
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "eval"))

from questions import QUESTIONS                                 # noqa: E402
from stage4_agent import DB, load_env, run_agent, set_variant    # noqa: E402

DEFAULT_Q = "中国石化和中国石油2023年的营业收入相差多少？"

# 题号 -> 问题文本，用来支持传 "q011" 这种简写
QUESTION_MAP = {q["id"]: q["question"] for q in QUESTIONS}


def classify(trace) -> str:
    """给这次运行归类。

    no_query : 一次 run_sql 都没执行 —— 【最危险】，答案肯定是编的
    queried  : 至少查了一次
    """
    sql_calls = [t for t in trace
                 if t["type"] == "tool" and t["tool"] == "run_sql"]
    return "no_query" if not sql_calls else "queried"


def main() -> None:
    load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise SystemExit("找不到 DEEPSEEK_API_KEY")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    # 最后一个参数如果是纯数字，当作「重复次数」
    n = 5
    if args and args[-1].isdigit():
        n = int(args.pop())

    # 剩下的第一个参数：题号（q011）→ 查表换成问题文本；否则直接当问题文本用
    #   ★ 这里踩过坑：一开始直接把 "q011" 当问题发给了模型，
    #     模型当然看不懂，于是 100% 「跳过查询」—— 全是假信号。
    if args:
        raw = args[0]
        question = QUESTION_MAP.get(raw, raw)
        if raw in QUESTION_MAP:
            print(f"[i] {raw} -> {question}")
        elif len(raw) < 12:
            print(f"[!] {raw!r} 既不是已知题号、又短得不像一个问题 —— 确认一下？")
    else:
        question = DEFAULT_Q

    variants = ["A_schema", "C_rich", "baseline"]

    print("=" * 78)
    print(f"数据库   : {DB}  （存在：{DB.exists()}）")
    print(f"问题     : {question}")
    print(f"每变体跑 : {n} 次")
    print("=" * 78)

    summary = {}
    for variant in variants:
        set_variant(variant)
        print(f"\n{'#' * 78}\n# 变体 {variant}\n{'#' * 78}")

        counts = {"no_query": 0, "queried": 0}
        for i in range(1, n + 1):
            try:
                answer, trace = run_agent(question, api_key, verbose=False)
            except Exception as exc:                      # noqa: BLE001
                print(f"  [{i}/{n}] 崩溃: {type(exc).__name__}: {exc}")
                continue

            kind = classify(trace)
            counts[kind] += 1
            flat = " ".join(answer.split())[:78]
            mark = "🔴 没查库" if kind == "no_query" else "  查了库"
            print(f"  [{i}/{n}] {mark}  {len(trace)} 步  {flat}")

        summary[variant] = counts

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("汇总：跳过查询的比例")
    print("-" * 78)
    print(f"{'变体':<12}{'没查库':>8}{'查了库':>8}{'总次数':>8}{'跳过率':>10}")
    print("-" * 78)
    for v, c in summary.items():
        total = c["no_query"] + c["queried"]
        rate = c["no_query"] / total if total else 0
        print(f"{v:<12}{c['no_query']:>8}{c['queried']:>8}{total:>8}{rate:>9.0%}")
    print("=" * 78)
    print()
    print("提示：no_query 的那几次，答案 100% 是编的 —— 它连数据库都没碰。")
    print("      如果这个比例不低，就说明「把完整 schema 放进提示」")
    print("      会让模型产生『我已经知道数据了』的错觉。")


if __name__ == "__main__":
    main()
