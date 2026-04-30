# 📚 Paper Daily

每日 AI 论文推荐工具。自动从多个学术源并行搜索最新论文，去重、五维排序、LLM 学术质量审核、生成中文推荐理由，并持久化到本地 SQLite 数据库。

## ✨ 功能

- 🔍 **多源并行搜索**：arXiv、OpenAlex、Semantic Scholar、CrossRef、Papers with Code（均无需认证）
- 🧹 **智能去重合并**：DOI → arXiv ID → 模糊标题匹配（rapidfuzz ≥0.88），多源字段按优先级合并
- 📊 **五维均匀加权排序**：相关性 × 时效性 × 影响力 × 新颖度 × **学术价值**，各占 0.20
- 🎓 **LLM 审稿人打分**：Anthropic-compatible API（默认 MiniMax-M2.7）评估论文学术质量，过滤缝合/水文
- 📝 **中文推荐理由**：复用同一个 LLM API 生成推荐语；无 API 时自动 fallback 到关键词启发式
- 🗄️ **本地持久化**：SQLite（WAL 模式）存储论文库、推荐历史、学术评估缓存
- 🔄 **可每日运行**：自动过滤已推荐论文，学术评估结果自动缓存避免重复调用 API

## 🚀 快速开始

### 安装

```bash
cd paper-daily
pip install -e .
```

或用 uv：

```bash
uv pip install -e .
```

### 配置（可选但强烈推荐）

复制示例环境文件并编辑：

```bash
cp .env.example .env
```

```env
# ---------------------------------------------------------------------------
# Unified LLM API (Anthropic-compatible)
#   Used for BOTH academic-value assessment AND Chinese recommendation reasons.
# ---------------------------------------------------------------------------
ACADEMIC_VALUE_URL=https://api.minimaxi.com/anthropic
ACADEMIC_VALUE_API_KEY=sk-...
ACADEMIC_VALUE_MODEL=MiniMax-M2.7

# ---------------------------------------------------------------------------
# OpenAlex email (optional, increases rate limit)
# ---------------------------------------------------------------------------
OPENALEX_EMAIL=your@email.com

# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------
PAPER_DAILY_DATA_DIR=./data

# ---------------------------------------------------------------------------
# Search & output tuning
# ---------------------------------------------------------------------------
PAPER_DAILY_TOP_N=10
PAPER_DAILY_MAX_RESULTS=50
PAPER_DAILY_DAYS_BACK=180

# ---------------------------------------------------------------------------
# Ranking weights (5-dimension, uniform by default)
# ---------------------------------------------------------------------------
W_RELEVANCE=0.20
W_RECENCY=0.20
W_IMPACT=0.20
W_NOVELTY=0.20
W_ACADEMIC_VALUE=0.20
```

**无 API key 也能用**：未配置 `ACADEMIC_VALUE_*` 时，学术价值维度以中性的 0.5 分参与排序；推荐理由自动 fallback 到关键词启发式生成。

### 运行

```bash
# 完整流程：搜索最近 180 天 → 去重 → 排序 → 生成推荐理由 → 输出 Top 10
paper-daily run

# 指定输出数量与回溯天数
paper-daily run -n 10 -d 180

# 使用自定义 .env 文件
paper-daily run --env /path/to/.env

# 详细日志
paper-daily run -v

# 查看历史推荐
paper-daily history
paper-daily history --days 7 -f markdown
paper-daily history --limit 100
```

## 📁 输出

运行后会在 `data/` 目录生成：

- `paper_daily.db` — SQLite 数据库（论文库 + 推荐历史 + 学术评估缓存）
- `recommendations_YYYY-MM-DD.md` — Markdown 推荐列表
- `recommendations_YYYY-MM-DD.json` — JSON 原始数据（含完整 `_scores` 明细）
- `recommendations_latest.md/json` — 最新推荐的快捷文件

### Console 输出示例

```
📚 Paper Daily Top 3 | 2026-04-30

1. 【One-for-All: A Universal Model Unifying Visual ...】(2026) arXiv
   🎓 学术价值: 8.5/10 | 提出统一视觉任务的全新范式，方法有理论支撑且实验充分
   💡 该论文提出了一种统一的视觉模型框架，能够同时处理分类、检测、分割等多个任务...
   🔗 https://arxiv.org/abs/2501.xxxxx

2. 【Efficient Reward Modeling for RLHF】...
   ...
```

## 🏗️ 架构与数据流

```
paper-daily run
    │
    ├── search.py          并行搜索 5 个学术源（按时间排序，最近优先）
    ├── dedup.py           DOI + arXiv ID + 模糊标题去重；字段按源优先级合并
    ├── pipeline.py        编排：预排 → 剪枝 → 学术评估 → 终排
    ├── ranker.py          五维评分引擎（所有维度归一化到 [0,1]）
    ├── academic_value.py  LLM 审稿人 0–10 分评估学术质量 + SQLite 缓存 + 无限重试
    ├── reviewer.py        复用同一个 Anthropic-compatible API / fallback 生成中文推荐理由
    ├── output.py          写入 SQLite + Markdown + JSON
    └── database.py        papers / recommendations / academic_cache 管理
```

### 排序逻辑详解

五维权重默认**均匀分配**，各占 **0.20**：

1. **相关性**：论文文本与主题关键词集的 Jaccard-like 最大重叠度。
2. **时效性**：≤30 天=1.0，≤90 天=0.9，≤180 天=0.8，之后指数衰减。
3. **影响力**：本批次内相对引用数的平方衰减 `(relative)^2`，确保只有最高引论文获得显著加分，新论文不因引用少被过度惩罚。
4. **新颖度**：从未推荐过=1.0，已推荐过=0.0。
5. **学术价值**：LLM 审稿人 0–10 分归一化到 [0,1]。

### 学术评估候选池

1. 对所有新论文做 4D 预排序（不含学术价值）
2. **剪掉排名最低的 20%**（淘汰明显不相关的）
3. 对剩余 **全部候选论文** 调用 LLM 审稿人评估
4. 结合学术价值做 5D 终排序，取 Top N

首次运行可能评估数十篇；后续运行大部分会命中 `academic_cache`，仅需 API 调用未缓存的新论文。

### 学术评估缓存

`academic_cache` 表以 `SHA256(title + abstract)` 为键永久缓存评估结果。若需重新评估某篇论文，删除对应缓存行即可：

```sql
DELETE FROM academic_cache WHERE content_hash = '...';
```

### 无限重试机制

学术评估 API 调用失败时自动重试，**没有最多尝试次数**：

- 退避间隔：`1s, 2s, 4s, 8s, 16s, 30s, 30s, ...`（cap 在 30 秒）
- 仅当成功解析出 `<score>` 标签时才退出循环
- 并发限制为 3，避免对 API 端点造成过大压力

## 🔎 搜索主题

默认覆盖 **14 个 AI 子领域**，每个主题取前 3 个关键词在 5 个源中搜索（每源最多 50 条，按时间降序）：

| Key | 子领域 | 关键词示例 |
|-----|--------|-----------|
| `llm` | 大语言模型 | large language model, LLM, transformer, reasoning |
| `vision` | 计算机视觉 | computer vision, diffusion model, image generation, segmentation |
| `rl` | 强化学习 | reinforcement learning, RLHF, PPO, Q-learning |
| `multimodal` | 多模态学习 | multimodal, vision-language model, cross-modal |
| `agent` | AI 智能体 | AI agent, autonomous agent, tool use, planning |
| `efficiency` | 效率与系统 | model efficiency, quantization, pruning, distillation |
| `genai` | 生成式 AI | generative AI, text-to-image, text-to-video, flow model |
| `embodied` | 具身智能 | embodied AI, robot learning, manipulation, humanoid robot |
| `autonomous_driving` | 自动驾驶 | autonomous driving, self-driving, end-to-end driving |
| `foundation_model` | 基础模型 | foundation model, pre-training, scaling law, emergent ability |
| `world_model` | 世界模型 | world model, environment model, predictive model |
| `slam` | SLAM | SLAM, simultaneous localization and mapping, visual odometry |
| `end_to_end` | 端到端学习 | end-to-end learning, end-to-end system, end-to-end training |
| `dl_theory` | 深度学习理论 | neural network theory, optimization landscape, representation learning |

修改 `src/paper_daily/config.py` 中的 `_default_topics()` 即可自定义。

## 🗄️ 数据库表结构

运行后自动创建 `data/paper_daily.db`（WAL 模式），包含三张表：

### `papers` — 论文库（去重后的所有论文）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `doi` | TEXT UNIQUE | DOI |
| `arxiv_id` | TEXT UNIQUE | arXiv ID |
| `title` | TEXT NOT NULL | 标题 |
| `authors` | TEXT | 作者列表（JSON 字符串） |
| `abstract` | TEXT | 摘要 |
| `year` | INTEGER | 发表年份 |
| `published_date` | TEXT | 发表日期（YYYY-MM-DD） |
| `venue` | TEXT | 期刊/会议 |
| `citation_count` | INTEGER | 引用数 |
| `url` | TEXT | 论文链接 |
| `sources` | TEXT | 搜索来源（逗号分隔） |
| `created_at` | TEXT | 入库时间 |

### `recommendations` — 推荐历史

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `paper_id` | INTEGER FK | 关联 `papers.id` |
| `recommend_date` | TEXT | 推荐日期（YYYY-MM-DD） |
| `rank` | INTEGER | 当天排名 |
| `score` | REAL | 五维综合总分 |
| `reason_zh` | TEXT | 中文推荐理由 |
| `academic_score` | REAL | 学术价值分数（0–1） |
| `created_at` | TEXT | 记录时间 |

### `academic_cache` — 学术评估缓存

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `content_hash` | TEXT UNIQUE | SHA256(title + abstract) |
| `title` | TEXT | 论文标题（方便人工查看） |
| `academic_score` | REAL NOT NULL | 学术分数（0–1） |
| `academic_reason` | TEXT | 中文评估理由 |
| `created_at` | TEXT | 缓存时间 |

### 常用查询示例

```bash
# 进入数据库
sqlite3 data/paper_daily.db
```

```sql
-- 查看今天推荐的 Top 10
SELECT r.rank, p.title, p.venue, r.score, r.academic_score, r.reason_zh
FROM recommendations r
JOIN papers p ON p.id = r.paper_id
WHERE r.recommend_date = date('now')
ORDER BY r.rank;

-- 查看某篇论文的历史推荐记录
SELECT r.recommend_date, r.rank, r.score, r.academic_score
FROM recommendations r
JOIN papers p ON p.id = r.paper_id
WHERE p.doi = '10.xxxx/xxxxx'
ORDER BY r.recommend_date DESC;

-- 按学术价值排序查看最近 7 天推荐
SELECT r.recommend_date, r.rank, p.title, r.academic_score, r.reason_zh
FROM recommendations r
JOIN papers p ON p.id = r.paper_id
WHERE r.recommend_date >= date('now', '-7 days')
ORDER BY r.academic_score DESC, r.recommend_date DESC
LIMIT 20;

-- 查看缓存命中率（辅助判断 API 开销）
SELECT COUNT(*) as total_cache_entries FROM academic_cache;

-- 查看某篇论文是否在缓存中
SELECT * FROM academic_cache WHERE title LIKE '%transformer%';

-- 查看每个来源贡献了多少唯一论文
SELECT p.sources, COUNT(*) as cnt
FROM papers p
GROUP BY p.sources;
```

## ⚙️ 高级配置

### 环境变量完整列表

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PAPER_DAILY_DATA_DIR` | `./data` | 数据目录 |
| `PAPER_DAILY_TOP_N` | `10` | 每天推荐数量 |
| `PAPER_DAILY_MAX_RESULTS` | `50` | 每源每主题最大搜索条数 |
| `PAPER_DAILY_DAYS_BACK` | `180` | 回溯天数 |
| `ACADEMIC_VALUE_URL` | — | 统一 LLM API base URL（Anthropic-compatible） |
| `ACADEMIC_VALUE_API_KEY` | — | 统一 LLM API key |
| `ACADEMIC_VALUE_MODEL` | `MiniMax-M2.7` | 统一 LLM 模型名 |
| `OPENALEX_EMAIL` | — | OpenAlex 邮箱（提高 rate limit） |
| `W_RELEVANCE` | `0.20` | 相关性权重 |
| `W_RECENCY` | `0.20` | 时效性权重 |
| `W_IMPACT` | `0.20` | 影响力权重 |
| `W_NOVELTY` | `0.20` | 新颖度权重 |
| `W_ACADEMIC_VALUE` | `0.20` | 学术价值权重 |

## ⚠️ 注意事项

1. **首次运行**需要一些时间（并行搜索 14 个主题 × 5 个源 + 对大量新论文做学术评估）。
2. **API 成本**：学术评估和推荐理由共用同一个 API。学术评估仅对未缓存的新论文调用，推荐理由仅对最终 Top N 调用。首次可能评估数十篇（每篇约 200–400 tokens），后续大部分命中 cache。
3. **无限重试**：学术评估内置指数退避重试（cap 30 秒，无上限），API 偶发卡顿时会自动恢复。若需中断可按 `Ctrl+C`。
4. **Rate limit**：个别源（如 Semantic Scholar）可能在短时间内返回 429，系统会跳过该源继续运行，不影响整体流程。
5. **中文推荐理由**默认使用 **fallback 规则**生成；配置 `ACADEMIC_VALUE_*` 后即可启用 LLM 生成更高质量的推荐语。

## 📄 License

MIT
