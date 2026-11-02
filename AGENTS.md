# AGENTS.MD

## 项目概述

TRAE Agent Chat：将本机 `traecli`（ACP 协议 Agent）封装为 HTTP 服务的最小聊天应用。

- 后端目录：`backend/` — `trae_http.py`（FastAPI 服务）、`trae.py`（ACP 客户端库）、`pyproject.toml`/`uv.lock`/`.venv`
- 前端目录：`frontend/` — `index.html`、`app.js`、`style.css` 及本地 vendored 的 marked/highlight.js

## 调用链

```
frontend/index.html (浏览器)
  → HTTP fetch → http://127.0.0.1:8300
    → backend/trae_http.py (uvicorn + FastAPI, AgentManager 持锁串行化)
      → backend/trae.py (ACP 客户端: TraeClient / _send_prompt / _shutdown)
        → stdio JSON-RPC → traecli acp serve (本地子进程)
          → 云端模型服务（仅 traecli 内部走网络）
```

## API

| 端点                   | 方法   | 说明                                                                                 |
| -------------------- | ---- | ---------------------------------------------------------------------------------- |
| `/api/health`        | GET  | 服务与 traecli 连接状态                                                                   |
| `/api/chat`          | POST | `{message, timeout?}` → SSE 流：`chunk`/`thought`/`tool` 实时增量 + 终态 `done`/`error` 事件 |
| `/api/session/reset` | POST | 开启新 ACP 会话，清空上下文                                                                   |

## 常用命令

```bash
# 启动后端（默认 127.0.0.1:8300，在 backend/ 目录下执行）
cd backend
uv run trae_http.py [--port 8300] [--model ...] [--yolo] [--debug]

# 快速验证
curl -s http://127.0.0.1:8300/api/health

# 交互式直连 traecli（不经 HTTP 层，调试 ACP 用）
cd backend && uv run trae.py
```

前端直接用浏览器打开 `frontend/index.html`，API 地址可在页面顶栏修改（持久化到 localStorage）。

<br />

## 必须遵循

- 生成的任何中间产物放到/tempfile 目录下

## 修改指引

- `backend/trae_http.py` 的 `AgentManager` 持有唯一子进程与会话，所有 prompt 经
  `asyncio.Lock` 串行化；不要引入并发 prompt。
- 子进程死亡时由 `chat()` 懒重启（`_ensure_started_locked`），勿在请求路径外手动 spawn。
- 回复采集在 `CollectingTraeClient.session_update`；新增需要透传给前端的事件，
  在此扩展采集（同时写入 `self.events` 队列实现 SSE 实时推送），并同步 `frontend/app.js` 的 `handleEvent()`。
- traecli 定位顺序：`--trae` 参数 > `ACP_TRAE_BIN` 环境变量 > PATH（`backend/trae.py::_resolve_trae_cli`）。
- prompt 超时默认 120s（`--prompt-timeout 0` 关闭），超时后发送 `conn.cancel` 并以
  SSE `error` 事件结束流；前端发 `timeout: 0` 关闭超时，浏览器断开会取消进行中的 prompt。

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

使用 uv + `backend/pyproject.toml`（依赖：`agent-client-protocol`、`fastapi`、`uvicorn`），
锁文件 `backend/uv.lock`。Python 版本要求 ≥ 3.13，虚拟环境位于 `backend/.venv/`。
注意 `.venv` 不可跨目录移动（内含绝对路径），移动项目后需 `cd backend && uv sync` 重建。
