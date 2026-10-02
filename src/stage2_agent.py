"""阶段 2： ReAct 循环 """
import json
import os
import re
import sqlite3
from pathlib import Path

import requests

DB = Path("data/demo.db")


# ============================================================
# 一、工具层：Agent 能做的"动作"
# ============================================================
# 关键认知：模型自己【不能】做任何事，它只会输出文字。
# "执行 SQL" 是你的 Python 代码干的。
# 模型做的事只有一件：决定【调哪个工具、传什么参数】。

def tool_list_tables(_arg: str = "") -> str:
    """列出数据库里所有表。"""
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
    conn = sqlite3.connect(DB)
    try:
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not cols:
            return f"表 {table} 不存在"
        return "\n".join(f"  {c[1]} {c[2]}" for c in cols)
    finally:
        conn.close()


FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach)\b",
    re.I,
)


def tool_run_sql(sql: str) -> str:
    """执行 SQL。注意这里做了两层安全限制。"""
    # 安全 1：只允许 SELECT —— 这是企业场景的硬要求，绝不能省
    if not sql.strip().lower().startswith(("select", "with")):
        return "❌ 只允许 SELECT 查询"
    # 安全 2：禁止危险关键字（防止用 CTE 绕过第一条）
    if FORBIDDEN.search(sql):
        return "❌ SQL 中包含被禁止的关键字"

    conn = sqlite3.connect(DB)
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as exc:
        # ★ 关键设计：把数据库的原始报错【原样返回给模型】
        #   模型看到 "no such column: xxx" 才知道自己哪里错了，才能改。
        #   如果把错误吞掉只返回"查询失败"，模型就失去了纠错的依据。
        return f"❌ SQL 执行错误：{type(exc).__name__}: {exc}"
    finally:
        conn.close()

    if not rows:
        return "（查询成功，但没有匹配的数据）"
    # 截断，防止 Result 太长把上下文撑爆
    shown = rows[:20]
    suffix = f"\n（共 {len(rows)} 行，只显示前 20 行）" if len(rows) > 20 else ""
    return json.dumps(shown, ensure_ascii=False) + suffix


TOOLS = {
    "list_tables": tool_list_tables,
    "describe_table": tool_describe_table,
    "run_sql": tool_run_sql,
}


# ============================================================
# 二、提示词：告诉模型"你有哪些工具、怎么调用"
# ============================================================
SYSTEM_PROMPT = """你是一个数据分析助手，可以通过调用工具来查询数据库。

【可用工具】

1. list_tables
   作用：列出数据库里所有表名
   输入：无（写 "无"）

2. describe_table
   作用：查看某张表有哪些字段
   输入：表名

3. run_sql
   作用：执行一条 SQLite 的 SELECT 查询，返回结果
   输入：完整的 SQL 语句

【输出格式 —— 必须严格遵守】

如果你还需要更多信息，输出固定三行：
Thought: <你的思考>
Action: <工具名>
Action Input: <工具输入>

如果你已经能回答用户的问题了，输出：
Thought: <你的思考>
Final Answer: <给用户的最终答案>

【重要规则】
- 一次只能调用一个工具
- Action 必须是上面三个工具名之一，不要自己发明
- 不要编造数据。所有数字必须来自 run_sql 的真实返回结果
- 如果查询出错，仔细看错误信息，修改 SQL 后重试
- 只能写 SELECT，不能修改数据
- 所有数值计算都要在 SQL 里完成。不要自己心算。比如问「A 和 B 相差多少」，就写SELECT a.value - b.value ... 把差值直接算出来，而不是查出两个原始值、然后自己减。数据库算的才是可信的。
- 【必须】所有数值计算都要在 SQL 里完成，不要自己心算。
"""


# ============================================================
# 三、解析层：把模型输出的文字解析成"动作"
# ============================================================

def parse_action(text: str):
    """返回 (工具名, 工具输入) 或 (None, 最终答案)。

    为什么要单独写这个函数：模型【不保证】遵守格式。
    解析失败是常态，必须显式处理。
    """
    if "Final Answer:" in text:
        return None, text.split("Final Answer:", 1)[1].strip()

    m_action = re.search(r"Action:\s*([A-Za-z_][\w]*)", text)
    if not m_action:
        return None, None                      # 格式完全不对

    name = m_action.group(1)

    m_input = re.search(r"Action Input:\s*(.+)", text, re.S)
    arg = m_input.group(1).strip() if m_input else ""
    if arg in ("", "无", "None", "(无)"):
        arg = ""

    return name, arg


# ============================================================
# 四、LLM 调用
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
# 五、★ 核心：ReAct 循环
# ============================================================

def run_agent(question: str, api_key: str, max_steps: int = 8, verbose: bool = True):
    """Agent 主循环。

    整个 Agent 的本质就在这个 for 循环里：
        模型输出 → 解析动作 → 执行工具 → 把结果塞回对话 → 再来一轮
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace = []                                  # 记录每一步，方便调试和分析

    for step in range(1, max_steps + 1):
        reply = call_llm(messages, api_key)

        if verbose:
            print(f"\n--- 第 {step} 步 ---")
            print(reply)

        name, arg = parse_action(reply)

        # 情况 1：模型给出了最终答案 → 结束
        if name is None and arg is not None:
            trace.append({"step": step, "type": "answer"})
            return arg, trace

        # 情况 2：模型格式不对 → 提醒它
        if name is None:
            messages.append({"role": "assistant", "content": reply})
            messages.append({
                "role": "user",
                "content": "你的输出不符合格式。请严格按 Thought/Action/Action Input "
                           "或 Thought/Final Answer 的格式重新输出。",
            })
            trace.append({"step": step, "type": "format_error"})
            continue

        # 情况 3：调了一个不存在的工具 → 告诉它有哪些
        if name not in TOOLS:
            observation = (f"❌ 没有名为 {name} 的工具。"
                           f"可用工具：{', '.join(TOOLS)}")
            trace.append({"step": step, "type": "bad_tool", "tool": name})
        else:
            # ★ 真正执行工具
            observation = TOOLS[name](arg)
            trace.append({
                "step": step, "type": "tool",
                "tool": name, "arg": arg,
                "observation": str(observation)[:300],
            })

        if verbose:
            print(f"→ 工具 {name} 的返回：\n{observation}")

        # ★★ 循环的关键：把「模型的思考」和「环境的反馈」都追加进对话历史
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": f"Observation: {observation}"})

    return f"（达到最大步数 {max_steps}，未能得出结论）", trace


def main():
    load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise SystemExit("找不到 DEEPSEEK_API_KEY")

    # 注意：这次的问题比阶段 1 更难，就是逼模型必须多步
    questions = [
        "2023年营业收入最高的公司是哪家？",
        "2023年净利润比2022年增长最多的公司是哪家？增长了多少？",
        "原油产量最高的那家公司，2023年的净利润是多少？",
    ]

    for q in questions:
        print("\n" + "=" * 72)
        print(f"问题：{q}")
        print("=" * 72)

        answer, trace = run_agent(q, api_key)

        print("\n" + "-" * 72)
        print(f"最终答案：{answer}")
        used = [t for t in trace if t["type"] == "tool"]
        print(f"共 {len(trace)} 步，其中工具调用 {len(used)} 次")


if __name__ == "__main__":
    main()