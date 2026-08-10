# Memory Hub — 跨智能体记忆中间件

> 泡芙 AI 生态的共享记忆层。另见：[Puff](https://github.com/Zhu070124/puff)（创意智能体）· [Workshop](https://github.com/Zhu070124/paofu-creative-workshop)（群聊）

> 为多智能体系统打造的轻量级共享记忆层。智能体有选择地贡献**经过筛选的洞察**——而不是原始对话转储。SQLite + FTS5，RESTful API，零依赖。

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![stdlib](https://img.shields.io/badge/依赖-0-brightgreen)]()
[![License](https://img.shields.io/badge/license-MIT-orange)](LICENSE)
[![Ecosystem](https://img.shields.io/badge/泡芙AI-生态-7C3AED)](https://github.com/Zhu070124)

---

## 要解决的问题

多智能体系统中，每个智能体对用户的理解各自为政。Agent A 知道用户喜欢简洁文风，Agent B 不知道。Agent C 对用户工作流有一个绝佳的观察——但其他智能体永远看不到。

现有方案的问题：
- **Mem0 / LangChain Memory** —— 全量对话转储。99% 是噪音，淹没了真正有用的信号
- **共享数据库** —— 没有筛选机制，智能体用原始闲聊污染共享池
- **手动用户画像文件** —— 过时、不实时、不跨智能体

---

## Memory Hub 的解决方案

```
POST /insight              GET /profile
  ┌──────────┐           ┌──────────────┐           ┌──────────┐
  │  Claude  │ ────────> │              │ <──────── │  Hermes  │
  │   Code   │           │  Memory Hub  │           │          │
  └──────────┘           │  (SQLite)    │           └──────────┘
                         │              │
  ┌──────────┐  GET /sync│  8 endpoints │ POST /stale ┌──────────┐
  │   Puff   │ <──────── │  63 条筛选后  │ ──────────> │   泡芙    │
  └──────────┘           │  的洞察       │             └──────────┘
                         └──────────────┘
```

三条设计原则：

1. **筛选优于倾倒。** 智能体先在自己的私有记忆中写作，只有确信值得分享的高质量观察才 `POST /insight`
2. **可信度分级。** 每条洞察被标记为 `confirmed`（用户确认）、`observed`（智能体观察）或 `speculative`（推测）。冲突被确定性解决，不忽略
3. **可追溯性。** 每条洞察记录来源智能体。如果某条洞察后来被证明是错的，你知道该找谁

---

## 数据模型

```sql
insights (
    content     TEXT,       -- 筛选后的洞察（≤500 字）
    source      TEXT,       -- 来源智能体
    lens        TEXT,       -- 分类: writing/tech/personality/habits/projects/general
    confidence  TEXT,       -- confirmed / observed / speculative
    priority    TEXT,       -- P0(永久) / P1(90天) / P2(30天)
    tags        TEXT,       -- 可搜索标签
    stale       INTEGER     -- 标记为已归档
)
-- FTS5 全文索引支持中英文混合搜索
```

### 画像分类

| 分类 | 追踪什么 | 示例 |
|---|---|---|
| `writing` | 风格偏好、体裁兴趣 | "偏好极简描写，不喜欢冗长形容词" |
| `tech` | 技术技能、项目偏好 | "Agent 方向，判断力 > 实现细节" |
| `personality` | 性格特质、MBTI、价值观 | "骄傲和柔软并存，反驳欲望强烈" |
| `habits` | 工作模式、作息习惯 | "清晨时段生产力最高" |
| `projects` | 活跃项目、目标 | "FDE 开发路线图，小说《玉兰烬》已完成" |
| `general` | 未分类观察 | -- |

---

## API

| 端点 | 方法 | 用途 |
|---|---|---|
| `POST /insight` | 写入 | 智能体贡献一条筛选后的洞察 |
| `GET /profile?lens=writing` | 查询 | 按分类拉取洞察 |
| `GET /sync?since=ISO_timestamp` | 同步 | 增量拉取（某时间点之后的新洞察） |
| `GET /sources` | 统计 | 各智能体贡献数量统计 |
| `GET /search?q=term` | 搜索 | 全文搜索所有洞察 |
| `POST /confirm` | 确认 | 提升可信度（observed → confirmed） |
| `POST /stale` | 归档 | 标记过期洞察等待清理 |
| `POST /archive` | 归档 | 归档所有 stale 洞察到 archive.jsonl |

---

## 快速开始

Memory Hub 是基础设施——先启动它，再启动其他智能体。

```bash
# 启动服务器
python hub.py serve
# 监听 http://127.0.0.1:8921

# CLI 使用
python client.py share "用户偏好极简风格" --source puff --lens writing
python client.py profile --lens tech,writing
python client.py search "Agent"
python client.py stats
```

Windows 下也可以双击 `hub.cmd`。

### Docker

```bash
docker-compose up -d
# SQLite 数据库挂载在 ./data/insights.db
```

---

## 为什么选 SQLite + FTS5？

- **零配置。** 不需要 Postgres、Redis、Docker。一个文件，一个进程。
- **全文搜索。** FTS5 原生支持中英文混合查询。
- **可移植。** 复制 `insights.db` 到另一台机器——这就是全部数据。
- **纯标准库。** `sqlite3` 和 `http.server` 都是 Python 标准库。不需要 `pip install`。
- **并发安全。** WAL 模式 + busy timeout + 写入重试。多智能体可同时读取，写入者遇到锁冲突自动重试。

---

## 性能

| 指标 | 数值 |
|---|---|
| 存储引擎 | SQLite + FTS5 |
| 洞察数量 | ~1,000（已测试） |
| 搜索延迟 | 毫秒级（FTS5 索引） |
| 写入延迟 | < 5 ms（WAL 模式） |
| 并发读 | 无限制（WAL） |
| 并发写 | 1（带重试的串行） |

---

## 冲突解决策略

当两个智能体为**同一分类**贡献**相似内容**（≥ 70% 文本重叠）时，系统检测到冲突并确定性解决：

1. **更高可信度获胜。** `confirmed` > `observed` > `speculative`
2. **更新的时间戳打破平局。** 可信度相同时，最近更新的获胜
3. **失败方标记 stale，而非删除。** 保留完整审计轨迹

---

## 文件结构

```
memory-hub/
├── hub.py                  # HTTP 服务器（纯标准库）
├── client.py               # 智能体调用的 CLI 客户端
├── hermes_sync.py          # 与 Hermes facts.db 双向同步
├── hub.cmd                 # Windows 启动器
├── insights.db             # SQLite 数据库（自动创建）
├── memory_hub.log          # 应用日志（自动创建）
├── tests/
│   └── test_insights.py    # 单元测试
├── Dockerfile
├── docker-compose.yml
├── README.md
└── LICENSE
```

---

## 安全规范

- **并发写保护：** WAL 模式 + 5 秒 busy timeout + 3 次指数退避重试
- **输入验证：** content ≤ 500 字符，lens/confidence/priority 严格枚举，请求体 ≤ 64KB
- **错误码：** 400（参数错误）/ 404（未找到）/ 409（冲突）/ 500（内部错误）

---

## 常见问题

| 症状 | 原因 | 解决 |
|---|---|---|
| 端口 8921 被占用 | 另一个 Memory Hub 实例在运行 | `netstat -ano | findstr :8921` → `taskkill /PID <PID> /F` |
| 数据库锁定 | 长时间写事务未提交 | 等 5-10 秒自动重试；或 `PRAGMA wal_checkpoint(TRUNCATE)` |
| FTS5 搜索无结果（中文） | 默认 tokenizer 不处理 CJK | 自动降级为 `LIKE '%term%'` |
| hermes_sync.py 连接被拒 | Memory Hub 未启动 | 先启动 `python hub.py serve`，确认 `curl http://127.0.0.1:8921/sources` |

---

## 未来规划

- **短期：** 用 `aiohttp` 替换单线程 `http.server`，支持 50+ req/s
- **中期：** 添加向量嵌入语义搜索，彻底解决 CJK 分词问题
- **长期：** LiteFS 分布式复制，多机同步

---

## 许可

MIT © 2026 朱郅（泡芙）
