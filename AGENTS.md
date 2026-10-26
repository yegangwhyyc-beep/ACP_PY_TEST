# AGENTS.MD

## 项目概述

TRAE Agent Chat：将本机 `traecli`（ACP 协议 Agent）封装为 HTTP 服务的最小聊天应用。

- 后端核心：`trae_http.py` — FastAPI 服务，管理 traecli ACP 子进程与单一长会话
- 前端页面：`index.html` — 纯静态单文件，通过 fetch 调用后端
- ACP 客户端库：`trae.py` — `TraeClient` 及工具函数，被 `trae_http.py` 导入

## 调用链

```
index.html (浏览器)
  → HTTP fetch → http://127.0.0.1:8300
    → trae_http.py (uvicorn + FastAPI, AgentManager 持锁串行化)
      → trae.py (ACP 客户端: TraeClient / _send_prompt / _shutdown)
        → stdio JSON-RPC → traecli acp serve (本地子进程)
          → 云端模型服务（仅 traecli 内部走网络）
```

## API

| 端点                   | 方法   | 说明                                                                   |
| -------------------- | ---- | -------------------------------------------------------------------- |
| `/api/health`        | GET  | 服务与 traecli 连接状态                                                     |
| `/api/chat`          | POST | `{message, timeout?}` → `{reply, session_id, thoughts, tool_events}` |
| `/api/session/reset` | POST | 开启新 ACP 会话，清空上下文                                                     |

## 常用命令

```bash
# 启动后端（默认 127.0.0.1:8300）
.venv/bin/python trae_http.py [--port 8300] [--model ...] [--yolo] [--debug]

# 快速验证
curl -s http://127.0.0.1:8300/api/health

# 交互式直连 traecli（不经 HTTP 层，调试 ACP 用）
.venv/bin/python trae.py
```

前端直接用浏览器打开 `index.html`，API 地址可在页面顶栏修改（持久化到 localStorage）。

## 修改指引

- `trae_http.py` 的 `AgentManager` 持有唯一子进程与会话，所有 prompt 经
  `asyncio.Lock` 串行化；不要引入并发 prompt。
- 子进程死亡时由 `chat()` 懒重启（`_ensure_started_locked`），勿在请求路径外手动 spawn。
- 回复采集在 `CollectingTraeClient.session_update`；新增需要透传给前端的事件，
  在此扩展采集并同步 `ChatResponse` 模型与 `index.html` 的 `addExtras()`。
- traecli 定位顺序：`--trae` 参数 > `ACP_TRAE_BIN` 环境变量 > PATH（`trae.py::_resolve_trae_cli`）。
- prompt 超时默认 120s（`--prompt-timeout 0` 关闭），超时后发送 `conn.cancel` 并返回 504。

## 已知坑（历史教训）

1. **ACP schema 判别字段必须完整**：构造 `TextContentBlock` 需 `type="text"`，
   `AgentMessageChunk` 需 `session_update="agent_message_chunk"`；缺失会导致
   错误响应静默序列化失败并挂起连接。
2. **ACP stdio agent 不能手动在终端输入**：必须由客户端作为子进程 spawn 驱动，
   非 JSON 输入会导致 JSON-RPC 解析错误。
3. **macOS GUI 应用看不到** **`~/.local/bin/uv`**：引用 uv 需使用绝对路径。
4. traecli 偶尔把内部横幅（如 "Skill descriptions were shortened"）作为 agent
   消息推送，`trae_http.py::_TRAECLI_BANNER_PREFIXES` 负责过滤，勿移除。

## 依赖管理

使用 uv + `pyproject.toml`（依赖：`agent-client-protocol`、`fastapi`、`uvicorn`），
锁文件 `uv.lock`。Python 版本要求 ≥ 3.13，虚拟环境位于 `.venv/`。
