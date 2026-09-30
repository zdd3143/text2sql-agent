import json
import os
import re
import sqlite3
from pathlib import Path

import requests

DB = Path("data/demo.db")


def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def get_schema(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    lines = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    ):
        cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
        col_desc = ", ".join(f"{c[1]} {c[2]}" for c in cols)
        lines.append(f"表 {name}({col_desc})")
    conn.close()
    return "\n".join(lines)


SYSTEM_PROMPT = """你是一个 SQL 生成器。

用户会给你数据库的表结构和一个问题。你要输出一条 SQLite 的 SELECT 语句。

要求：
1. 只输出 SQL，不要任何解释、不要 markdown 代码块
2. 只能写 SELECT，不能写 INSERT/UPDATE/DELETE/DROP
3. 需要跨表时用 JOIN"""


def call_llm(schema: str, question: str, api_key: str) -> str:
    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",
                 "content": f"【表结构】\n{schema}\n\n【问题】\n{question}"},
            ],
            "temperature": 0,
            "max_tokens": 512,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def clean_sql(text: str) -> str:
    text = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text.strip(), flags=re.I)
    return text.strip()


def run_sql(sql: str, db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def main():
    load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise SystemExit("找不到 DEEPSEEK_API_KEY，请在 .env 里配置")

    schema = get_schema(DB)
    print("=== 数据库表结构 ===")
    print(schema)
    print()

    questions = [
        "2023年营业收入最高的公司是哪家？",
        "中国石油2023年的净利润是多少？",
        "2023年净利润比2022年增长最多的公司是哪家？",
        "原油产量最高的公司2023年营业收入是多少？",
        "2023年营业收入超过1万亿的公司有几家？",
    ]

    for q in questions:
        print("=" * 70)
        print(f"问题：{q}")

        raw = call_llm(schema, q, api_key)
        sql = clean_sql(raw)
        print(f"生成的 SQL：{sql}")

        try:
            rows = run_sql(sql, DB)
            print(f"执行结果（{len(rows)} 行）：{rows[:5]}")
        except Exception as exc:
            print(f"❌ 执行失败：{type(exc).__name__}: {exc}")
        print()


if __name__ == "__main__":
    main()