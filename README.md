# 🌟 WikiNova

> 基于 IMA + Obsidian + Wiki 的本地知识库问答 Agent

WikiNova 是一个智能知识管理助手，专注于**本地知识库问答**。它能自动从 IMA 同步内容，写入 Obsidian，通过 LLM 生成互联 Wiki 知识图谱，并基于知识库回答你的问题。

## 核心架构

```
IMA (腾讯 AI 知识库)                    用户笔记 (Obsidian)
    ↓ IMA OpenAPI 拉取                      ↓ 手动编写
LLM 总结层 (结构化 frontmatter + 正文)       ↓
    ↓                                        ↓
Obsidian Vault (Nanobot/Inbox/)  ←──── 主数据源 (Source of Truth)
    ↓ 每日 21:30 自动同步 + 实时轮询
ObsidianWikiSync (SHA-256 增量检测)
    ↓ 仅处理新增/修改的文件
WikiGenerator (LLM 隔离 Agent Turn)
    ↓ 一篇笔记 → 3-8 个互联 Wiki 页面
Wiki Pages (workspace/wiki/pages/)
    ↓ BM25 搜索 + [[wikilink]] 图谱
Agent Q&A (wiki_search → wiki_read → 综合回答)
    ↓ [wiki:slug] 引用标注
用户 (微信 / 飞书 / Web)
```

## 功能特性

### 知识同步

| 链路 | 机制 | 频率 |
|------|------|------|
| IMA → Obsidian | `IMAIngestPipeline`: 拉取笔记+知识库 → LLM 总结 → 写入 Inbox | 每日 21:30 |
| Obsidian → Wiki | `ObsidianWikiSync`: SHA-256 增量检测 → LLM 生成互联 Wiki | 每日 21:30 + 实时轮询 |
| 对话 → Wiki | `WikiEvolution`: 从对话历史提取新事实更新 Wiki | 每 6 小时 |
| 记忆整合 | Dream: 两阶段记忆合并 | 每 2 小时 |

### 增量更新保障

- **IMA 去重**: 基于 `note_id` / `media_id` 黑名单，已处理的 IMA 内容不重复拉取
- **Obsidian 去重**: 基于 SHA-256 文件哈希，未修改的笔记跳过生成
- **Evolution 去重**: 基于 history.jsonl 行号游标 + diff gate，无实际文件变更不推进游标

### Wiki 系统

- **Karpathy 风格生成**: 一篇源笔记 → 3-8 个互联 Wiki 页面（主题页、概念页、对比页、实体页）
- **`[[wikilink]]` 语法**: 页面间通过 wikilink 建立知识图谱
- **BM25 搜索**: 纯 Python 实现，支持中英文关键词匹配和前缀搜索
- **自主索引**: 每次生成后自动更新 `index.md` 导航页
- **Git 审计**: 每次 wiki 演进都有 commit 记录
- **健康检查**: 自动检测矛盾、孤立页面、缺失链接、标签不一致

### 知识问答

- **QA Gate**: 关键词分类器，拦截代码生成/创意写作等 off-topic 请求
- **搜索优先**: 强制 `wiki_search` → `wiki_read` → 综合回答的工作流
- **引用标注**: 每条事实声明标注 `[wiki:slug]`，前端渲染为可点击 HoverCard
- **知识源优先级**: Wiki < Obsidian Vault（vault 是 source of truth）

### 聊天平台

- **微信**: iLink API，QR 扫码登录
- **飞书**: App ID + Secret 配置
- **WebSocket**: 内置连接，支持 WebUI

### 前端

- **Chat**: 对话界面，支持 Markdown 渲染、代码高亮、引用预览
- **Wiki**: 知识浏览 + 搜索 + 演进日志 + 页面编辑 + 导入
- **Channels**: 微信 + 飞书配置
- **Models**: 模型配置与切换
- **Skills**: 技能管理

## 快速开始

### 1. 安装

```bash
git clone https://github.com/harrycjs/wikinova.git
cd wikinova
pip install -e .
```

### 2. 配置

编辑 `~/.nanobot/config.json`：

```json
{
  "tools": {
    "ima": {
      "enabled": true,
      "clientId": "你的 IMA Client ID",
      "apiKey": "你的 IMA API Key"
    },
    "obsidian": {
      "enabled": true,
      "vaultPath": "C:/Users/你的用户名/Documents/Obsidian Vault",
      "nanobotRoot": "Nanobot",
      "syncMode": "poll",
      "pollIntervalS": 60
    },
    "wiki": {
      "enabled": true
    }
  }
}
```

### 3. 启动

```bash
nanobot gateway
```

打开 http://127.0.0.1:8765/ 访问 WebUI。

## 定时任务

| 任务 | 时间（北京时间） | 作用 |
|---|---|---|
| Dream | 每 2 小时 | 两阶段记忆合并：对话历史 → 工作记忆 → 长期记忆 |
| Heartbeat | 每 30 分钟 | 检查 HEARTBEAT.md 中的活跃任务 |
| Wiki Evolve | 每 6 小时 | 从对话历史提取新事实/偏好/实体更新 Wiki |
| Knowledge Sync AM | **每日 12:25** | IMA 拉取 → Obsidian 总结 → Wiki 生成全链路 |
| Knowledge Sync PM | **每日 17:40** | 同上，傍晚再跑一次 |

## 微信文章正文抓取 (wxmp_operator)

IMA OpenAPI 对微信公众号文章返回 `210005 GetNoteContent not author`（OpenAPI token 不是 KB 所有者），所以需要单独扫码登录拿 cookies。

**一次性配置：** 在 Channels 页面 → **wxmp** Tab → 点「扫码登录」 → Edge 弹出 → 手机扫码 → 自动保存 96h 凭证。

**自动 fallback 链路（`pipeline.py:_fetch_note_content`）：**

1. `get_doc_content` — IMA 服务端提取的文本
2. `get_media_info` + 直接 URL 抓取 — 静态网页可用
3. **wxmp_operator** — 微信文章专用路径，用持久化的 operator cookies 抓 SSR HTML，提取 `<div id="js_content">` 内的正文

凭证过期（>96h）后自动跳过微信文章，pipeline 不会写入占位笔记。需要重新扫码。

### 手动重新扫码

```bash
nanobot channels login wxmp_platform --force
```

## Wiki API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/wiki/list` | GET | 列出所有 Wiki 页面 |
| `/api/wiki/page?slug=` | GET | 读取单个页面 |
| `/api/wiki/search?q=` | GET | BM25 搜索 |
| `/api/wiki/evolution` | GET | 演进审计日志 |
| `/api/wiki/edit` | POST | 编辑页面 |
| `/api/wiki/delete?slug=` | POST | 软删除页面 |
| `/api/wiki/import` | POST | 从 URL/文件/文本导入 |
| `/api/wiki/regenerate` | POST | 触发演进（异步） |
| `/api/wiki/lint` | POST | 健康检查 |

## 项目结构

```
nanobot/
├── agent/
│   ├── loop.py              # AgentLoop: 核心处理引擎
│   ├── runner.py            # AgentRunner: LLM 对话循环
│   ├── qa_gate.py           # QA 意图分类器
│   ├── wiki/                # Wiki 子系统
│   │   ├── generator.py     # LLM 生成 Wiki 页面
│   │   ├── store.py         # 页面持久化存储
│   │   ├── querier.py       # BM25 搜索引擎
│   │   ├── sync.py          # Obsidian → Wiki 增量同步
│   │   ├── evolution.py     # 对话历史 → Wiki 演进
│   │   ├── tools.py         # Agent-facing Wiki 工具
│   │   └── prompts/         # 生成/索引/检查 prompt 模板
│   └── knowledge/
│       ├── cron.py          # 知识同步定时任务
│       └── pipeline.py      # IMA → Obsidian 流水线
├── providers/               # LLM 提供商 (DeepSeek, OpenAI, Anthropic…)
├── channels/                # 聊天平台 (微信, 飞书, WebSocket…)
├── webui/ws_http.py         # WebSocket + HTTP API
└── config/schema.py         # 配置 Schema

webui/src/
├── components/wiki/         # WikiView, WikiHoverCard
├── lib/markdown/            # citation-parser (引用解析)
└── components/              # Chat, Channels, Models, Skills
```

## 技术栈

- **后端**: Python 3.11+, asyncio, Pydantic
- **前端**: React 18 + TypeScript + Tailwind CSS + Vite
- **LLM**: DeepSeek (默认), OpenAI, Anthropic 等
- **搜索**: 纯 Python BM25（无外部依赖）
- **存储**: 原子文件写入 (tempfile + fsync) + Git 审计

## 致谢

本项目基于 [nanobot](https://github.com/HKUDS/nanobot) 项目构建。感谢 nanobot 提供了优秀的 Agent 框架、通道系统和工具架构，使得 WikiNova 能够专注于知识库问答这一核心场景。

## License

MIT
