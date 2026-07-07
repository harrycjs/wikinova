# 🌟 WikiNova

> 基于 IMA + Obsidian + Wiki 的本地知识库问答 Agent

WikiNova 是一个智能知识管理助手，专注于**本地知识库问答**。它能自动从 IMA 同步内容，写入 Obsidian，生成互联 Wiki 知识图谱，并基于知识库回答你的问题。

## 核心架构

```
IMA (腾讯 AI 知识库)
    ↓ IMA OpenAPI 拉取
LLM 总结层
    ↓ 生成结构化笔记
Obsidian Vault (主数据源)
    ↓ 监听/轮询
LLM Wiki (Karpathy 风格)
    ↓ 自主生成 [[wikilink]] 互联页面
Agent Q&A
    ↓ 基于 Wiki + Obsidian 生成回答
用户 (微信/飞书/Web)
```

## 功能特性

### 知识同步
- **IMA → Obsidian**: 每 6 小时自动同步 IMA 新内容，LLM 总结后写入 Obsidian Inbox
- **Obsidian → Wiki**: 监听 vault 变更，自动更新 Wiki 知识图谱
- **自进化**: 每 6 小时从对话历史提取新事实，更新 Wiki

### Wiki 系统
- Karpathy 风格：LLM 自主生成互相链接的 Wiki 页面
- `[[wikilink]]` 语法，涌现式知识图谱
- Git 追踪，每次演进都有 commit
- BM25 关键词搜索

### 聊天平台
- **微信**: QR 扫码登录，context_token 磁盘持久化
- **飞书**: App ID + Secret 配置
- **Web**: 内置 WebUI，Jobs 极简风设计

### 前端
- Channel 页面：IMA + Obsidian + 微信 + 飞书配置
- Model 页面：模型配置
- Skill 页面：技能管理
- Plugin 页面：插件管理
- Wiki 页面：知识浏览 + 搜索 + 演进日志

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
      "vaultPath": "D:/path/to/obsidian/vault"
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

| 任务 | 频率 | 作用 |
|---|---|---|
| Dream | 每 2 小时 | 合并对话历史到记忆文件 |
| Heartbeat | 每 30 分钟 | 检查活跃任务 |
| Wiki Evolve | 每 6 小时 | 从对话历史提取新事实更新 Wiki |
| Knowledge Sync | 每 6 小时 | IMA 新内容 → Obsidian → Wiki |

## 技术栈

- **后端**: Python 3.11+, asyncio
- **前端**: React 18 + TypeScript + Tailwind CSS + shadcn/ui
- **LLM**: 支持 DeepSeek、OpenAI、Anthropic 等
- **聊天**: 微信 iLink API、飞书 OpenAPI

## 致谢

本项目基于 [nanobot](https://github.com/HKUDS/nanobot) 项目构建。感谢 nanobot 提供了优秀的 Agent 框架、通道系统和工具架构，使得 WikiNova 能够专注于知识库问答这一核心场景。

## License

MIT