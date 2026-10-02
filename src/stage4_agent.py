"""阶段 4：工具集设计 —— 三种给模型"单位信息"的方案对照。

阶段 3 的基线暴露了两个稳定失败：
    q006「营收超过1亿元的公司有几家」→ 说 2 家（实际 6 家），
         而且它编了一个"revenue 字段单位为万元"的解释来自圆其说。
    q011「两家公司营收相差多少」→ 它查了两行原始值，自己在脑子里做减法。

本阶段针对「单位信息缺失」（q006）做三个对照实验：

    baseline  —— 什么都不改
    A_schema  —— 把带注释的完整 schema 直接塞进系统提示（顺带省掉探索步骤）
    B_sample  —— 新增 sample_rows 工具，让模型自己看数据数量级（不依赖人工注释）
    C_rich    —— describe_table 返回时附上单位说明（保留探索流程）

用法：
    python src/stage4_agent.py                  # 默认 baseline，跑内置问题
    python src/stage4_agent.py A_schema         # 指定变体
    python src/stage3_eval.py --variant A_schema --repeat 3   # 用评估集对比
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import requests

DB = Path("data/demo.db")

# 当前变体，由 set_variant() 修改
VARIANT = "baseline"


def set_variant(name: str) -> None:
    global VARIANT
    VARIANT = name


# ============================================================
# 一、元信息：单位 / 口径说明
# ============================================================
#  ★ 关键问题：这些注释从哪来？
#    真实生产里来自【数据字典 / 元数据平台】，或者人工整理。
#    本项目的注释是手写的 —— 这本身就是"依赖人工"的代价，
#    也是 B 方案（采样）想绕开的东西。

COLUMN_NOTES = {
    "companies.company_id":  "公司主键",
    "companies.name":        "公司简称，如「中国石油」",
    "financials.company_id": "关联 companies.company_id",
    "financials.year":       "会计年度（如 2023 表示 2023 财年）",
    "financials.revenue":    "营业收入，单位：亿元（人民币）",
    "financials.net_profit": "归属于母公司股东的净利润，单位：亿元",
    "financials.total_assets": "总资产，单位：亿元",
    "production.year":       "数据年份",
    "production.product":    "产品类型：原油 / 天然气",
    "production.amount":     "产量数值，单位见 unit 字段",
    "production.unit":       "产量单位，如「亿桶」「亿立方米」",
}


def _note(table: str, column: str) -> str:
    return COLUMN_NOTES.get(f"{table}.{column}", "")


# ============================================================
# 二、工具层
# ============================================================

def tool_list_tables(_arg: str = "") -> str:
    """列出所有表。保留一个用不到的 _arg —— 分发表统一按 TOOLS[name](arg) 调用。"""
    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return ", ".join(r[0] for r in rows)
    finally:
        conn.close()


def tool_describe_table(table: str) -> str:
    """查看某张表的字段。

    变体 C_rich：在每个字段后面附上单位 / 口径说明。
    """
    conn = sqlite3.connect(DB)
    try:
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not cols:
            return f"表 {table} 不存在"
        lines = []
        for c in cols:
            line = f"  {c[1]} {c[2]}"
            if VARIANT == "C_rich":
                n = _note(table, c[1])
                if n:
                    line += f"    -- {n}"
            lines.append(line)
        return "\n".join(lines)
    finally:
        conn.close()


def tool_sample_rows(arg: str) -> str:
    """返回某张表的前几行真实数据，让模型自己判断字段的数量级和单位。

    参数格式：表名 [行数]，例如 "financials 3"

    为什么这个方案可能更通用：
        它不依赖任何人去写"单位是亿元"这句话 ——
        模型看到 revenue 的值是 32122 而不是 32122000000，
        自己就能推断出单位不可能是"元"。
    """
    parts = arg.split()
    table = parts[0] if parts else ""
    n = 3
    if len(parts) > 1 and parts[1].isdigit():
        n = min(int(parts[1]), 5)

    conn = sqlite3.connect(DB)
    try:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if not cols:
            return f"表 {table} 不存在"
        rows = conn.execute(f"SELECT * FROM {table} LIMIT {n}").fetchall()
    except Exception as exc:                      # noqa: BLE001
        return f"❌ 查询失败：{type(exc).__name__}: {exc}"
    finally:
        conn.close()

    out = [f"表 {table} 的前 {len(rows)} 行（字段顺序：{', '.join(cols)}）"]
    for r in rows:
        out.append("  " + " | ".join(str(v) for v in r))
    return "\n".join(out)


FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach)\b", re.I
)


def tool_run_sql(sql: str) -> str:
    """执行 SQL。两层安全限制 + 把数据库报错原样返回给模型。"""
    if not sql.strip().lower().startswith(("select", "with")):
        return "❌ 只允许 SELECT 查询"
    if FORBIDDEN.search(sql):
        return "❌ SQL 中包含被禁止的关键字"

    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as exc:                      # noqa: BLE001
        # ★ 原样返回报错：模型看到 "no such column: xxx" 才知道怎么改
        return f"❌ SQL 执行错误：{type(exc).__name__}: {exc}"
    finally:
        conn.close()

    if not rows:
        return "（查询成功，但没有匹配的数据）"
    shown = rows[:20]
    suffix = f"\n（共 {len(rows)} 行，只显示前 20 行）" if len(rows) > 20 else ""
    return json.dumps(shown, ensure_ascii=False) + suffix


def build_tools() -> dict:
    tools = {
        "list_tables": tool_list_tables,
        "run_sql": tool_run_sql,
    }
    # A 变体：schema 已在系统提示里，describe_table 会返回无注释版本、造成信息冲突
    if VARIANT != "A_schema":
        tools["describe_table"] = tool_describe_table
    if VARIANT == "B_sample":
        tools["sample_rows"] = tool_sample_rows
    return tools

# ============================================================
# 三、提示词
# ============================================================

ROLE = "你是一个数据分析助手，可以通过调用工具来查询数据库。"

TOOL_DOCS = """【可用工具】

1. list_tables
   作用：列出数据库里所有表名
   输入：无（写 "无"）

2. describe_table
   作用：查看某张表有哪些字段
   输入：表名

3. run_sql
   作用：执行一条 SQLite 的 SELECT 查询，返回结果
   输入：完整的 SQL 语句"""

TOOL_DOCS_SAMPLE = """
4. sample_rows
   作用：查看某张表的前几行【真实数据】。
        想知道某个字段的单位或数量级时，很有用 ——
        看到 revenue 的值是 32122 而不是 32122000000，就能判断单位。
   输入：表名，可选行数。例如 "financials 3" """

FORMAT = """【输出格式 —— 必须严格遵守】

如果你还需要更多信息，输出固定三行：
Thought: <你的思考>
Action: <工具名>
Action Input: <工具输入>

如果你已经能回答用户的问题了，输出：
Thought: <你的思考>
Final Answer: <给用户的最终答案>"""

RULES = """【重要规则】
- 一次只能调用一个工具
- Action 必须是上面列出的工具名之一，不要自己发明
- 不要编造数据。所有数字必须来自 run_sql 的真实返回结果
- 如果查询出错，仔细看错误信息，修改 SQL 后重试
- 只能写 SELECT，不能修改数据
- 【必须】所有数值计算都要在 SQL 里完成，不要自己心算。
  比如问「A 和 B 相差多少」，就写 SELECT a.value - b.value ... 把差值直接算出来，
  而不是查出两个原始值、然后自己减。
- 【单位】如果字段的单位不明确，先想办法弄清楚（看字段说明、或采样看数据数量级），
  不要在回答里编造单位。确实无法确定就写明「单位未知」。
- 如果数据库里根本没有相关数据，直接说明「数据库中没有相关数据」，不要编造。"""


def build_annotated_schema() -> str:
    """把整个数据库的 schema 拼成带注释的文本（A_schema 变体用）。"""
    conn = sqlite3.connect(DB)
    lines = []
    try:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ):
            lines.append(f"表 {name}:")
            for c in conn.execute(f"PRAGMA table_info({name})").fetchall():
                note = _note(name, c[1])
                lines.append(f"  {c[1]} {c[2]}" + (f"    -- {note}" if note else ""))
            lines.append("")
    finally:
        conn.close()
    return "\n".join(lines)


def build_system_prompt() -> str:
    parts = [ROLE, TOOL_DOCS]
    if VARIANT == "B_sample":
        parts.append(TOOL_DOCS_SAMPLE)
    if VARIANT == "A_schema":
        parts.append("【数据库表结构（已包含单位说明，可直接使用）】")
        parts.append(build_annotated_schema())
    parts.append(FORMAT)
    parts.append(RULES)
    return "\n\n".join(parts)


# ============================================================
# 四、解析
# ============================================================

def parse_action(text: str):
    """返回 (工具名, 输入) 或 (None, 最终答案)。"""
    if "Final Answer:" in text:
        return None, text.split("Final Answer:", 1)[1].strip()

    m_action = re.search(r"Action:\s*([A-Za-z_][\w]*)", text)
    if not m_action:
        return None, None
    name = m_action.group(1)

    m_input = re.search(r"Action Input:\s*(.+)", text, re.S)
    arg = m_input.group(1).strip() if m_input else ""
    if arg in ("", "无", "None", "(无)"):
        arg = ""
    return name, arg


# ============================================================
# 五、LLM
# ============================================================

def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def call_llm(messages, api_key: str) -> str:
    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "deepseek-chat",
            "messages": messages,
            "temperature": 0,
            "max_tokens": 1024,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ============================================================
# 六、ReAct 循环
# ============================================================

def run_agent(question: str, api_key: str, max_steps: int = 8, verbose: bool = True):
    tools = build_tools()
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": question},
    ]
    trace = []

    for step in range(1, max_steps + 1):
        reply = call_llm(messages, api_key)
        if verbose:
            print(f"\n--- 第 {step} 步 ---")
            print(reply)

        name, arg = parse_action(reply)

        if name is None and arg is not None:
            trace.append({"step": step, "type": "answer"})
            return arg, trace

        if name is None:
            messages.append({"role": "assistant", "content": reply})
            messages.append({
                "role": "user",
                "content": "你的输出不符合格式。请严格按 Thought/Action/Action Input "
                           "或 Thought/Final Answer 的格式重新输出。",
            })
            trace.append({"step": step, "type": "format_error"})
            continue

        if name not in tools:
            observation = (f"❌ 没有名为 {name} 的工具。"
                           f"可用工具：{', '.join(tools)}")
            trace.append({"step": step, "type": "bad_tool", "tool": name})
        else:
            observation = tools[name](arg)
            trace.append({
                "step": step, "type": "tool",
                "tool": name,
                "arg": arg,                       # ★ 不截断：评估要重新执行它
                "observation": str(observation)[:300],
            })

        if verbose:
            print(f"→ 工具 {name} 的返回：\n{observation}")

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": f"Observation: {observation}"})

    return f"（达到最大步数 {max_steps}，未能得出结论）", trace


# ============================================================
# 七、单独跑（不进评估）
# ============================================================

def main():
    load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise SystemExit("找不到 DEEPSEEK_API_KEY")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        set_variant(args[0])

    print(f"变体：{VARIANT}")
    if VARIANT == "A_schema":
        print("（系统提示里已包含带注释的完整 schema）")
    elif VARIANT == "B_sample":
        print("（工具集里多了 sample_rows）")
    elif VARIANT == "C_rich":
        print("（describe_table 会返回单位说明）")

    questions = [
        "2023年营业收入超过1亿元的公司有几家？",       # ★ 主靶子
        "中国石化和中国石油2023年的营业收入相差多少？",   # 次靶子
    ]

    for q in questions:
        print("\n" + "=" * 72)
        print(f"问题：{q}")
        print("=" * 72)
        answer, trace = run_agent(q, api_key)
        print("\n" + "-" * 72)
        print(f"最终答案：{answer}")
        calls = [t for t in trace if t["type"] == "tool"]
        print(f"共 {len(trace)} 步，工具调用 {len(calls)} 次")
        print(f"用到的工具：{[t['tool'] for t in calls]}")


if __name__ == "__main__":
    main()