"""建一个示例数据库：石油上市公司财务与产量。"""
import sqlite3
from pathlib import Path

DB = Path("data/demo.db")
DB.parent.mkdir(parents=True, exist_ok=True)
DB.unlink(missing_ok=True)          

# ---- 表结构设计 ----
SCHEMA = """
CREATE TABLE companies (
    company_id  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    stock_code  TEXT,
    sector      TEXT
);

CREATE TABLE financials (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL,
    year          INTEGER NOT NULL,
    revenue       REAL,        -- 营业收入（亿元）
    net_profit    REAL,        -- 归母净利润（亿元）
    total_assets  REAL,        -- 总资产（亿元）
    FOREIGN KEY (company_id) REFERENCES companies(company_id)
);

CREATE TABLE production (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL,
    year        INTEGER NOT NULL,
    product     TEXT,          -- 原油 / 天然气 / 成品油
    amount      REAL,          -- 产量
    unit        TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(company_id)
);
"""

COMPANIES = [
    (1, "中国石油", "601857", "油气开采"),
    (2, "中国石化", "600028", "炼化销售"),
    (3, "中国海油", "600938", "海上油气"),
    (4, "恒力石化", "600346", "炼化销售"),
    (5, "荣盛石化", "002493", "炼化销售"),
    (6, "中海油服", "601808", "油田服务"),
]

FINANCIALS = [
    # (company_id, year, revenue, net_profit, total_assets)
    (1, 2022, 32391.7, 1487.4, 26735.0),
    (1, 2023, 30110.1, 1611.5, 27524.0),
    (2, 2022, 33181.7,  661.5, 20266.0),
    (2, 2023, 32122.2,  604.6, 20241.0),
    (3, 2022,  4222.3, 1417.0,  9243.0),
    (3, 2023,  4166.1, 1238.4,  9732.0),
    (4, 2022,  2223.7,   23.4,  2605.0),
    (4, 2023,  2347.9,   69.1,  2752.0),
    (5, 2022,  2890.9,   33.6,  3659.0),
    (5, 2023,  3250.7,   11.6,  3821.0),
    (6, 2022,   357.0,   23.3,   791.0),
    (6, 2023,   448.6,   30.1,   832.0),
]

PRODUCTION = [
    # (company_id, year, product, amount, unit)
    (1, 2023, "原油",     9.36, "亿桶"),
    (1, 2023, "天然气", 4828.0, "亿立方米"),
    (2, 2023, "原油",     2.79, "亿桶"),
    (2, 2023, "天然气", 1337.0, "亿立方米"),
    (3, 2023, "原油",     5.15, "亿桶"),
    (3, 2023, "天然气",  727.0, "亿立方米"),
]


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.executescript(SCHEMA)
    cur.executemany(
        "INSERT INTO companies (company_id, name, stock_code, sector) "
        "VALUES (?,?,?,?)",
        COMPANIES,
    )
    cur.executemany(
        "INSERT INTO financials (company_id, year, revenue, net_profit, total_assets) "
        "VALUES (?,?,?,?,?)",
        FINANCIALS,
    )
    cur.executemany(
        "INSERT INTO production (company_id, year, product, amount, unit) "
        "VALUES (?,?,?,?,?)",
        PRODUCTION,
    )
    conn.commit()

    print(f"数据库已建好：{DB.resolve()}\n")

    
    for table in ("companies", "financials", "production"):
        n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        cols = [d[1] for d in cur.execute(f"PRAGMA table_info({table})")]
        print(f"  {table:12s} {n:3d} 行   字段: {', '.join(cols)}")

    print("\n--- 试一条 SQL ---")
    rows = cur.execute("""
        SELECT c.name, f.revenue
        FROM financials f JOIN companies c ON c.company_id = f.company_id
        WHERE f.year = 2023
        ORDER BY f.revenue DESC
    """).fetchall()
    for name, rev in rows:
        print(f"    {name:8s} {rev:>10,.1f} 亿元")

    conn.close()


if __name__ == "__main__":
    main()