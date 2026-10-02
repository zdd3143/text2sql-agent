"""阶段 5：把数据库扩展到接近真实企业库的规模。

为什么要这么做
--------------
阶段 4 的结论是「把整个 schema 塞进系统提示效果最好」（A_schema 变体 15/15 全对），
但那是【3 张表】的前提。真实企业库有几百张表，全塞进提示会：

    1. 超出上下文长度
    2. 让模型在无关表里迷失（表一多，注意力就被稀释了）

所以必须先制造「规模压力」，才能验证「选表」到底有没有用。

设计要点
--------
1. 原有的 3 张核心表（companies / financials / production）**原样保留**，
   所以阶段 1-4 的评估集和实验结论仍然有效。

2. 新增的表分两类：

   【陷阱表】名字像财务、字段也像财务，但数据是**错的**（备份快照 / 母公司口径 / 未完成的临时表）。
             它们的存在意义是：让「选错表」有实际后果——答案会错。
             真实企业库里这种表遍地都是，是 Text-to-SQL 出错的主要原因之一。

   【无关表】正常的企业业务表（考勤、库存、物流……），
             作用是**占上下文**，考验「选表」能不能把它们筛掉。

3. 输出到独立的 data/big.db，**不污染 demo.db**——
   这样阶段 1-4 的实验仍然可以在原库上复现。

用法
----
    python src/stage5_build_bigdb.py

    # 然后让 agent / eval 用大库：
    $env:T2S_DB="data/big.db"
    python src/stage4_agent.py A_schema
    python src/stage3_eval.py --variant A_schema --repeat 1
"""
import shutil
import sqlite3
from pathlib import Path

SRC = Path("data/demo.db")
DST = Path("data/big.db")

# ============================================================
# 一、陷阱表：名字和字段都像财务表，但数据【不是】正确口径
# ============================================================
TRAP_TABLES = {
    "fin_bak_2023": (
        "财务数据备份表（2023 年快照，仅供审计调阅；字段与正式表一致但数据可能滞后）",
        "company_id INTEGER, year INTEGER, revenue REAL, net_profit REAL, total_assets REAL",
    ),
    "fin_parent_only": (
        "母公司口径财务数据（非合并报表；与合并数差异很大，不可用于公司整体分析）",
        "company_id INTEGER, year INTEGER, revenue REAL, net_profit REAL",
    ),
    "tmp_fin_import": (
        "财务系统导入临时表（数据不完整，勿用于对外口径）",
        "company_id INTEGER, year INTEGER, revenue REAL, net_profit REAL",
    ),
}

# 故意填【错误】的数字：和正式表都不一样，选了它答案就错
TRAP_ROWS = {
    "fin_bak_2023": [
        (1, 2023, 29999.9, 1600.0, 27000.0),
        (2, 2023, 31999.9, 600.0, 20000.0),
        (3, 2023, 4100.0, 1200.0, 9700.0),
    ],
    "fin_parent_only": [
        (1, 2023, 18500.0, 950.0),
        (2, 2023, 12000.0, 300.0),
        (3, 2023, 2600.0, 800.0),
    ],
    "tmp_fin_import": [
        (1, 2023, 0.0, 0.0),
        (2, 2023, 0.0, 0.0),
    ],
}

# ============================================================
# 二、无关业务表：正常的企业表，但与本项目的财务问答无关
# ============================================================
NOISE_TABLES = {
    "employees":        "员工基本信息（工号、姓名、部门、入职日期）",
    "departments":      "部门组织架构",
    "attendance":       "考勤打卡记录",
    "salary":           "薪酬发放记录",
    "training":         "员工培训记录",
    "inventory":        "库存台账",
    "warehouse":        "仓库信息",
    "logistics":        "物流运输记录",
    "contracts":        "合同台账",
    "suppliers":        "供应商信息",
    "customers":        "客户信息",
    "orders":           "销售订单",
    "equipment":        "设备台账",
    "maintenance":      "设备维修记录",
    "safety_incidents": "安全事故记录",
    "environment":      "环保监测数据",
    "projects":         "工程项目台账",
    "budget":           "年度预算表",
    "expenses":         "费用报销记录",
    "tax_records":      "纳税记录",
    "oil_price":        "国际油价历史行情",
    "exchange_rate":    "汇率历史行情",
}

NOISE_COLUMNS = "code TEXT, name TEXT, amount REAL, record_date TEXT, remark TEXT"

CORE_TABLES = ("companies", "financials", "production")


def list_tables(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def build_trap_tables(cur) -> None:
    for name, (_desc, cols) in TRAP_TABLES.items():
        cur.execute(f"DROP TABLE IF EXISTS {name}")
        cur.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, {cols})")

        rows = TRAP_ROWS[name]
        n_cols = len(rows[0]) + 1                     # +1 是主键 id
        placeholders = ",".join("?" * n_cols)
        cur.executemany(
            f"INSERT INTO {name} VALUES ({placeholders})",
            [(i + 1, *row) for i, row in enumerate(rows)],
        )


def build_noise_tables(cur, multiplier: int = 1) -> None:
    """建干扰表。multiplier 用来批量放大规模，用来找「全塞进提示词」的崩溃点。

    放大方式：给表名加时间后缀，把每张表复制成 N 份。
        比如 employees -> employees_q1 / employees_q2 / employees_2019 / ...
    这正好模拟了真实企业库里「每年/每季度留一份快照表」的做法 ——
    那也是真实库里表数量爆炸的主要原因之一。
    """
    # 11 个时间后缀；第 12 份开始用 _v12 / _v13… 兜底
    suffixes = ([f"_q{q}" for q in range(1, 5)]
                + [f"_{y}" for y in range(2019, 2026)])

    for name, desc in NOISE_TABLES.items():
        for k in range(multiplier):
            if k == 0:
                table = name
            elif k <= len(suffixes):
                table = f"{name}{suffixes[k - 1]}"
            else:
                table = f"{name}_v{k}"

            cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {NOISE_COLUMNS})"
            )
            cur.executemany(
                f"INSERT INTO {table} (code, name, amount, record_date, remark) "
                f"VALUES (?,?,?,?,?)",
                [
                    (f"{table[:6].upper()}{i:03d}", f"{desc}-样例{i}",
                     i * 1000.0, f"2023-0{i}-15", "示例数据")
                    for i in range(1, 4)
                ],
            )


def render_schema(conn, tables) -> str:
    """把整个库的 schema 渲染成文本，用于估算"全塞进提示词"的规模。"""
    lines = []
    for t in tables:
        lines.append(f"表 {t}:")
        for c in conn.execute(f"PRAGMA table_info({t})").fetchall():
            lines.append(f"  {c[1]} {c[2]}")
    return "\n".join(lines)


def main() -> None:
    import sys

    # 可选参数：干扰表倍数。默认 1（= 28 张表）。
    # 传 13 大约得到 290 张表，用来找「全塞」的崩溃点。
    mult = 1
    if len(sys.argv) > 1:
        try:
            mult = int(sys.argv[1])
        except ValueError:
            raise SystemExit(
                "用法：python src/stage5_build_bigdb.py [干扰表倍数]\n"
                "  1  = 28 张表（默认，阶段 5.1）\n"
                "  13 = 约 290 张表（找崩溃点）\n"
                "  40 = 约 880 张表（模拟真实企业库规模）"
            )

    if not SRC.exists():
        raise SystemExit(f"找不到 {SRC}，请先跑：python src/stage0_build_db.py")

    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, DST)          # 从 demo.db 复制，保留原有 3 张核心表

    conn = sqlite3.connect(DST)
    cur = conn.cursor()

    build_trap_tables(cur)
    build_noise_tables(cur, multiplier=mult)
    conn.commit()

    tables = list_tables(conn)

    # ---- 验证核心表没被破坏 ----
    print(f"数据库已扩展：{DST.resolve()}")
    print(f"共 {len(tables)} 张表（干扰表倍数 = {mult}）\n")

    print("核心表（阶段 1-4 用的，必须完整保留）：")
    for t in CORE_TABLES:
        if t not in tables:
            raise SystemExit(f"  ✗ 核心表 {t} 丢失！")
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cols = len(cur.execute(f"PRAGMA table_info({t})").fetchall())
        print(f"  ✓ {t:<14} {n:>3} 行 / {cols} 个字段")

    # ---- 列出表（多了就只列一部分）----
    def kind_of(t):
        if t in CORE_TABLES:
            return "核心"
        if t in TRAP_TABLES:
            return "★陷阱"
        return "  无关"

    print(f"\n全部 {len(tables)} 张表：")
    if len(tables) <= 40:
        for t in tables:
            cols = len(cur.execute(f"PRAGMA table_info({t})").fetchall())
            print(f"  {kind_of(t)}  {t:<22} {cols:>2} 个字段")
    else:
        # 表太多，只列核心 + 陷阱 + 头尾各 6 张
        head = ["companies", "financials", "production"] + list(TRAP_TABLES)
        for t in head:
            cols = len(cur.execute(f"PRAGMA table_info({t})").fetchall())
            print(f"  {kind_of(t)}  {t:<22} {cols:>2} 个字段")
        print(f"  ...（省略 {len(tables) - len(head) - 6} 张无关表）...")
        for t in tables[-6:]:
            cols = len(cur.execute(f"PRAGMA table_info({t})").fetchall())
            print(f"  {kind_of(t)}  {t:<22} {cols:>2} 个字段")

    # ---- 估算 schema 规模 ----
    schema_text = render_schema(conn, tables)
    n_core_cols = sum(len(cur.execute(f"PRAGMA table_info({t})").fetchall())
                      for t in CORE_TABLES)
    n_all_cols = sum(len(cur.execute(f"PRAGMA table_info({t})").fetchall())
                     for t in tables)
    conn.close()

    # 只含 3 张核心表时的 schema 规模（用于对比）
    core_only_chars = n_core_cols * 22 + len(CORE_TABLES) * 12

    print(f"\n{'=' * 64}")
    print("规模对比")
    print("-" * 64)
    print(f"  表数量        {len(tables):>6} 张     （核心只有 3 张）")
    print(f"  字段总数      {n_all_cols:>6} 个     （核心只有 {n_core_cols} 个）")
    print(f"  schema 全文   {len(schema_text):>6,} 字符")
    print(f"  粗估 token    {len(schema_text):>6,}       （中文一字约 1 token）")
    print(f"  相对核心 3 表 放大约 {len(schema_text) / max(core_only_chars, 1):.0f} 倍")
    print()
    print("  ⚠️ 这份 schema 每一轮 ReAct 循环都要原样重发一次。")
    print("     如果平均 3 步，就是 3 倍的成本 —— 这就是「全塞」的代价。")
    print(f"{'=' * 64}")
    print()
    print("下一步：")
    print('  $env:T2S_DB="data/big.db"')
    print(f"  python src\\stage3_eval.py --variant A_schema --repeat 1")
    print()
    print("  # 想换规模就重跑本脚本（会覆盖 big.db）：")
    print("  python src\\stage5_build_bigdb.py 1     # 28 张表")
    print("  python src\\stage5_build_bigdb.py 13    # 约 290 张表")
    print("  python src\\stage5_build_bigdb.py 40    # 约 880 张表")


if __name__ == "__main__":
    main()