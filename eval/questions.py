"""Text-to-SQL 评估集。"""

QUESTIONS = [
    # ---------------- 简单查询 ----------------
    {
        "id": "q001",
        "category": "simple",
        "question": "2023年营业收入最高的公司是哪家？",
        "gold_sql": """
            SELECT c.name
            FROM companies c
            JOIN financials f ON f.company_id = c.company_id
            WHERE f.year = 2023
            ORDER BY f.revenue DESC
            LIMIT 1
        """,
    },
    {
        "id": "q002",
        "category": "simple",
        "question": "中国石油2023年的净利润是多少？",
        "gold_sql": """
            SELECT f.net_profit
            FROM financials f
            JOIN companies c ON c.company_id = f.company_id
            WHERE c.name = '中国石油' AND f.year = 2023
        """,
    },
    {
        "id": "q003",
        "category": "simple",
        "question": "中国海油2023年的总资产是多少？",
        "gold_sql": """
            SELECT f.total_assets
            FROM financials f
            JOIN companies c ON c.company_id = f.company_id
            WHERE c.name = '中国海油' AND f.year = 2023
        """,
    },
    {
        "id": "q004",
        "category": "simple",
        "question": "2023年营业收入最低的公司是哪家？",
        "gold_sql": """
            SELECT c.name
            FROM companies c
            JOIN financials f ON f.company_id = c.company_id
            WHERE f.year = 2023
            ORDER BY f.revenue ASC
            LIMIT 1
        """,
    },

    # ---------------- ★ 单位陷阱（本项目的特色）----------------
    {
        "id": "q005",
        "category": "unit",
        "question": "2023年营业收入超过1万亿的公司有几家？",
        "gold_sql": """
            SELECT COUNT(*)
            FROM financials
            WHERE year = 2023 AND revenue > 10000
        """,
    },
    {
        "id": "q006",
        "category": "unit",
        "question": "2023年营业收入超过1亿元的公司有几家？",
        "gold_sql": """
            SELECT COUNT(*)
            FROM financials
            WHERE year = 2023 AND revenue > 1
        """,
    },

    # ---------------- 聚合 ----------------
    {
        "id": "q007",
        "category": "aggregate",
        "question": "2023年这6家公司的平均营业收入是多少？",
        "gold_sql": """
            SELECT AVG(revenue)
            FROM financials
            WHERE year = 2023
        """,
    },
    {
        "id": "q008",
        "category": "aggregate",
        "question": "2023年营业收入超过1万亿的公司有哪些？",
        "gold_sql": """
            SELECT c.name
            FROM companies c
            JOIN financials f ON f.company_id = c.company_id
            WHERE f.year = 2023 AND f.revenue > 10000
        """,
    },

    # ---------------- 多表 JOIN ----------------
    {
        "id": "q009",
        "category": "join",
        "question": "原油产量最高的公司，2023年的营业收入是多少？",
        "gold_sql": """
            SELECT f.revenue
            FROM production p
            JOIN financials f ON f.company_id = p.company_id AND f.year = p.year
            WHERE p.product = '原油' AND p.year = 2023
            ORDER BY p.amount DESC
            LIMIT 1
        """,
    },
    {
        "id": "q010",
        "category": "join",
        "question": "天然气产量最高的公司，2023年的净利润是多少？",
        "gold_sql": """
            SELECT f.net_profit
            FROM production p
            JOIN financials f ON f.company_id = p.company_id AND f.year = p.year
            WHERE p.product = '天然气' AND p.year = 2023
            ORDER BY p.amount DESC
            LIMIT 1
        """,
    },
    {
        "id": "q011",
        "category": "join",
        "question": "中国石化和中国石油2023年的营业收入相差多少？",
        "gold_sql": """
            SELECT MAX(f.revenue) - MIN(f.revenue)
            FROM financials f
            JOIN companies c ON c.company_id = f.company_id
            WHERE f.year = 2023 AND c.name IN ('中国石化', '中国石油')
        """,
    },

    # ---------------- 多步（Agent 的主场）----------------
    {
        "id": "q012",
        "category": "multi_step",
        "question": "2023年净利润比2022年增长最多的公司是哪家？",
        "gold_sql": """
            SELECT c.name
            FROM financials f23
            JOIN financials f22 ON f22.company_id = f23.company_id AND f22.year = 2022
            JOIN companies c ON c.company_id = f23.company_id
            WHERE f23.year = 2023
            ORDER BY (f23.net_profit - f22.net_profit) DESC
            LIMIT 1
        """,
    },
    {
        "id": "q013",
        "category": "multi_step",
        "question": "2023年净利润比2022年下降最多的公司是哪家？",
        "gold_sql": """
            SELECT c.name
            FROM financials f23
            JOIN financials f22 ON f22.company_id = f23.company_id AND f22.year = 2022
            JOIN companies c ON c.company_id = f23.company_id
            WHERE f23.year = 2023
            ORDER BY (f23.net_profit - f22.net_profit) ASC
            LIMIT 1
        """,
    },

    # ---------------- 不可答（数据库里根本没有）----------------
    {
        "id": "q014",
        "category": "unanswerable",
        "question": "中国石油2023年的员工人数是多少？",
        "gold_sql": None,       # 数据库里没有员工数字段
    },
    {
        "id": "q015",
        "category": "unanswerable",
        "question": "2024年营业收入最高的公司是哪家？",
        "gold_sql": None,       # 数据只有 2022 / 2023
    },
]