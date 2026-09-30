# 自然语言查数据库（Text-to-SQL Agent）

> 从零手写的 Text-to-SQL Agent。**不依赖 LangChain / LangGraph**，
> 自己实现 ReAct 循环、工具调用与 SQL 执行反馈。
> 重点是**失败归因**与**评估驱动的优化**。

<p>
<img alt="python" src="https://img.shields.io/badge/Python-3.12-blue">
<img alt="framework" src="https://img.shields.io/badge/framework-none-brightgreen">
<img alt="progress" src="https://img.shields.io/badge/progress-3%2F8-orange">
</p>

---

## 30 秒看懂

| 项 | 结果 |
|---|---|
| 数据库 | SQLite，3 张表 / 24 行（公司 / 财务 / 产量），含外键关联 |
| 核心链路 | 问题 → **ReAct 循环**（思考 → 行动 → 观察）→ 生成 SQL → 执行 → 纠错 → 自然语言回答 |
| **Agent 的本质** | **模型（决策）+ 工具（行动）+ 循环（多步）** |
| 当前进度 | 阶段 0-2：从"一次生成 SQL"到"手写 Agent 循环" |

**已发现的三个问题**（详见「实验与发现」）

1. 干净的小 schema 上，**一次生成 SQL 就能答对 4/5** —— Agent 的价值不在这种场景
2. **最危险的失败是"单位理解错"**：SQL 不报错、返回 0、看起来完全正常
3. **Agent 知道自己不知道，却没办法查证** —— 工具的信息密度决定它的上限

---

## 这是什么

用户用中文提问，系统自己决定查哪些表、写 SQL、执行、发现错误后自己改，
最后用自然语言回答。

```
问题："原油产量最高的那家公司，2023年的净利润是多少？"
  ↓
[ReAct 循环]
  Thought:  先看看有哪些表
  Action:   list_tables
  Observation: companies, financials, production
  Thought:  需要看 production 的字段
  Action:   describe_table
  Action Input: production
  Observation: id, company_id, year, product, amount, unit
  Thought:  查原油产量最高的公司
  Action:   run_sql
  Action Input: SELECT company_id, amount FROM production
                WHERE product='原油' ORDER BY amount DESC LIMIT 1
  Observation: [[1, 9.36, "亿桶"]]
  Thought:  是 company_id=1，再查它的净利润
  Action:   run_sql
  Action Input: SELECT c.name, f.net_profit FROM companies c
                JOIN financials f ON f.company_id=c.company_id
                WHERE c.company_id=1 AND f.year=2023
  Observation: [["中国石油", 1611.5]]
  Final Answer: 原油产量最高的公司是中国石油（9.36亿桶），
                其2023年净利润为1611.5亿元。
```

**注意它分了两步查**，而不是硬写一条大 SQL —— 这正是 Agent 的价值：
**可以把问题拆成几步，每步基于上一步的真实结果决定下一步。**

---

## 为什么不用 LangChain / LangGraph

整套机制一共 100 行左右：一个 `for` 循环 + 一张工具分发表。
自己写一遍才知道框架到底替你做了什么。

**具体收获**：
- 明白了"工具调用"的本质是**你的代码在执行，模型只负责决定调哪个**
- 明白了 `messages` 数组为什么会越来越长（模型思考和工具反馈都被追加进去了）
- 明白了**工具返回什么信息，直接决定模型能不能纠错**

---

## 实验与发现

> 这一节是项目的重点——不是「写了多少代码」，而是「验证了什么、发现了什么」。

### ① 干净的小 schema 上，"一次生成"就够了

**做法**：把 3 张表的完整 schema 塞进提示词，让模型一次生成 SQL，执行，不循环。

**结果**：5 个问题**答对 4 个**，包括一道需要**自连接**的题（"净利润增长最多的公司"）。

| 问题 | 结果 |
|---|---|
| 2023年营收最高的公司 | ✅ 中国石化 |
| 中国石油2023年净利润 | ✅ 1611.5 |
| 2023年净利润增长最多的公司 | ✅ 中国石油 +124.1（自连接）|
| 原油产量最高公司的营收 | ✅ 30110.1 |
| **营收超万亿的公司数** | ❌ **说 0 家，实际 2 家** |

**结论**：**Agent 的价值不在"干净的小库"上。**
这个 schema 只有 3 张表、字段名直白（`revenue` / `net_profit`）、数据干净——
一次生成就够用了。

真正的企业库是：表名像 `T_FIN_2023Q4_BAK`，字段是 `A0101`、`yysr` 这种代号，
同一个"营业收入"在三张表里口径不同。**那才是 Agent 的主场。**

### ② 最危险的失败不是语法错，而是"单位理解错" ⭐

问题：「2023年营业收入超过 **1万亿** 的公司有几家？」

模型生成的 SQL：

```sql
SELECT COUNT(*) FROM financials
WHERE year = 2023 AND revenue > 1000000000000    -- 10^12，它以为单位是"元"
```

**但数据单位是「亿元」**，正确写法应该是：

```sql
... WHERE revenue > 10000                        -- 1 万亿 = 10000 亿元
```

**正确答案 2 家（中国石油、中国石化），它返回 0 家。**

**为什么这类错误最危险**：

| | 语法错（字段名写错） | **单位错** |
|---|---|---|
| SQL 能执行吗 | ❌ 报错 | ✅ **正常执行** |
| 有异常吗 | ✅ 有 | ❌ **没有** |
| 返回什么 | 错误信息 | **一个看起来正常的结果 `0`** |
| 你能发现吗 | 一眼发现 | **得自己算一遍才知道** |

> **崩溃你知道要修；返回 0 你还以为"本来就没有公司超过一万亿"。**

### ③ Agent 知道自己不知道，却没有工具去查证 ⭐⭐

引入 ReAct 循环后，3 个问题**全部答对**。但看它三次回答的结尾：

```
"营业收入为 32122.2（单位与表中一致）"
"增长了 124.1（单位与数据库中的净利润单位一致）"
"净利润为 1611.5（单位与表中一致，通常为亿元）"     ← 它在猜
```

**它三次都主动声明"不知道单位"**，第 3 次甚至补了句"通常为亿元"——
**它在猜，而且知道自己是在猜。**

**但它的工具给不了答案**：

```
describe_table(financials) →
  revenue REAL        ← 只有字段名和类型，没有单位、没有口径、没有样例
  net_profit REAL
```

更关键的是：**它从头到尾没有运行过 `SELECT * FROM financials LIMIT 3`**——
**它从来没"看过"数据长什么样**，只知道字段叫 `revenue`，
不知道这个数是 `32122` 还是 `32122000000`。

> **两个结论**：
> 1. **加循环修不了单位问题** —— SQL 执行成功、没有报错，
>    循环里没有任何信息告诉它单位错了。它需要的是**更好的上下文**，不是更多的尝试机会。
> 2. **工具的信息密度，决定 Agent 的能力上限。**

### ④ 每次都要重新探索 schema，完全重复

三个问题，每个都花了 3-4 步在 `list_tables` + `describe_table` 上，
**而 schema 是静态的，三次探索结果一模一样。**

→ 应该把 schema 直接放进系统提示（阶段 3 解决）。
**但一旦 schema 大到放不下，就变成了更难的问题**（阶段 4）。

---

## 技术栈

| 环节 | 用的东西 |
|---|---|
| 数据库 | SQLite（标准库，零依赖）|
| 决策模型 | DeepSeek API |
| Agent 框架 | **无** —— 手写 ReAct 循环 |
| HTTP | requests |

---

## 进度

- [x] 阶段 0：环境 + 建库（3 张表，含外键）
- [x] 阶段 1：最笨的 Text-to-SQL（理解"为什么一次不够"）
- [x] 阶段 2：**手写 ReAct 循环**（Agent 的本质）
- [ ] 阶段 3：工具集设计（补单位 / 口径 / 样例数据）
- [ ] 阶段 4：Schema 选择（表多了怎么办）
- [ ] 阶段 5：**评估体系**（执行准确率 / 一次通过率）
- [ ] 阶段 6：接入 RAG（数值 + 原因一起答）
- [ ] 阶段 7：服务化 + 演示

---

## 目录结构

```
src/
  stage0_build_db.py   建库：3 张表 + 示例数据
  stage1_naive.py      一次生成 SQL（基线，会失败）
  stage2_agent.py      手写 ReAct 循环 + 工具集

data/
  demo.db              SQLite 数据库（.gitignore 忽略，可重建）

.env                   API 密钥（.gitignore 忽略）
```

---

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python src/stage0_build_db.py     # 建库（含示例数据）
python src/stage1_naive.py        # 基线：一次生成 SQL
python src/stage2_agent.py        # Agent：ReAct 循环
```

在根目录建 `.env`：

```
DEEPSEEK_API_KEY=sk-xxxxxxxx
```

> ⚠️ `.env` 已在 `.gitignore` 中，**密钥不会被提交**。

---

## 关键设计决策

| 决策 | 理由 |
|---|---|
| **手写 ReAct 而不用 LangGraph** | 机制一共 100 行；自己写才知道框架替你做了什么 |
| **工具统一接收一个字符串参数** | 分发表统一用 `TOOLS[name](arg)` 调用，不用为每个工具写特例 |
| **数据库报错原样返回给模型** | 模型看到 `no such column: xxx` 才知道怎么改；吞掉错误它就失去纠错依据 |
| **只允许 SELECT，且禁用危险关键字** | 企业场景的硬要求。两层检查防 CTE 绕过 |
| **工具返回结果截断到 20 行** | 防止 Result 太长把上下文撑爆 |
| **max_steps = 8** | 循环必须有上限，否则会失控烧 token |
| **temperature = 0** | SQL 生成要确定性，不需要创造性 |

---

## 已知局限

- 数据库只有 **3 张表 / 24 行**，远小于真实企业库
- **数据里没有单位信息**（`revenue` 是亿元但没有标注），
  已导致一次静默错误
- 评估只有 **5 个问题的人工观察**，还没有自动化评估集（阶段 5 解决）
- 每次问答都重复探索 schema，**浪费 3-4 步**（阶段 3 解决）
- 没有处理**歧义问题**（"去年"是哪一年？"营收"是合并口径还是母公司口径？）
- 没有多轮对话能力（每次都是独立问题）

---

## 下一步

- [ ] **阶段 3：工具集设计** —— 让 `describe_table` 返回单位、口径和**样例数据**，
      并验证能否修掉"单位理解错"这个静默错误
- [ ] 阶段 4：Schema 选择（表多了怎么筛）
- [ ] 阶段 5：**评估体系** —— 执行准确率、有效 SQL 率、
      **以及"跑通了但答错了"的检出率**（专门针对静默错误）

---

## 许可与数据来源

- 代码：MIT
- 数据：示例数据，用于跑通链路；后续接入公开财报数据