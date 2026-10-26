# TRAE Agent Chat 时序图

参与者：`浏览器`（index.html）、`FastAPI`（trae_http.py 路由层）、`AgentManager`、`Client`（CollectingTraeClient ⊃ TraeClient，同一对象两层）、`conn`（acp 库连接）、`traecli`（子进程）、`云端模型`

## ① 服务启动（lifespan）

```
python trae_http.py        uvicorn/lifespan      AgentManager         conn(acp库)        traecli子进程
     │                          │                     │                   │                   │
     │ main(): 解析参数          │                     │                   │                   │
     │ 构建全局 manager ────────>│                     │                   │                   │
     │ uvicorn.run(app)         │                     │                   │                   │
     │                          │ startup:            │                   │                   │
     │                          │ manager.start() ───>│ async with lock   │                   │
     │                          │                     │ _start_locked()   │                   │
     │                          │                     │ ── resolve bin ────────────────────> │ (trae.py::_resolve_trae_cli)
     │                          │                     │ ── spawn ─────────────────────────> │ traecli acp serve
     │                          │                     │                   │<═ stdin/stdout ═>│
     │                          │                     │ connect_to_agent(client, pipes)     │
     │                          │                     │ ── initialize ───>│── JSON-RPC ─────>│
     │                          │                     │ <─ protocol_version ───────────────│
     │                          │                     │ ── new_session ──>│── session/new ─>│
     │                          │                     │ <─ session_id ─────────────────────│
     │                          │ yield（启动完成，失败被 suppress，不阻塞服务）            │
```

## ② /api/chat 主流程（核心）

```
浏览器            FastAPI            AgentManager         Client              conn                traecli            云端模型
  │                 │                    │                 │                   │                   │                  │
  │ POST /api/chat  │                    │                 │                   │                   │                  │
  │ {message} ─────>│ chat()             │                 │                   │                   │                  │
  │                 │ message 为空? ──是──> 400（结束）      │                   │                   │                  │
  │                 │ manager.chat() ───>│ async with lock │                   │                   │                  │
  │                 │                    │ alive()?        │                   │                   │                  │
  │                 │                    │  └─否: _ensure_started_locked()     │                   │                  │
  │                 │                    │     (重启子进程+initialize+new_session)                  │                  │
  │                 │                    │ client.reset()  │ 清空三个缓冲列表   │                   │                  │
  │                 │                    │                 │                   │                   │                  │
  │                 │                    │ _send_prompt() ────────────────────>│ conn.prompt()    │                  │
  │                 │                    │                 │                   │── session/prompt>│                  │
  │                 │                    │                 │                   │   (stdin NDJSON) │── 推理请求 ─────>│
  │                 │                    │                 │                   │                   │<─ 流式生成 ──────│
  │                 │                    │                 │                   │                   │                  │
  │                 │                    │   ════════ 流式 session/update 通知（多条，循环） ════════                  │
  │                 │                    │                 │                   │<─ notification ──│                  │
  │                 │                    │                 │ session_update()  │                   │                  │
  │                 │                    │                 │ ├ ThoughtChunk    │                   │                  │
  │                 │                    │                 │ │  → thought_chunks                   │              │
  │                 │                    │                 │ ├ ToolCallStart   │                   │                  │
  │                 │                    │                 │ │  → tool_events   │                   │                  │
  │                 │                    │                 │ └ MessageChunk    │                   │                  │
  │                 │                    │                 │    → reply_chunks（横幅过滤）         │                  │
  │                 │                    │                 │ super().session_update()            │                  │
  │                 │                    │                 │  └─ 控制台打印日志(trae.py)          │                  │
  │                 │                    │                 │                   │                   │                  │
  │                 │                    │   ════════ 可选：权限请求分支（仅非 --yolo 时交互） ════════             │
  │                 │                    │                 │                   │<─ request_permission ──│           │
  │                 │                    │                 │ TraeClient.request_permission()     │                  │
  │                 │                    │                 │  ├ --yolo: 自动选 allow_once ──────>│                  │
  │                 │                    │                 │  └ 否则: 终端列出选项等待输入 ───────>│                  │
  │                 │                    │                 │                   │                   │                  │
  │                 │                    │                 │                   │<─ prompt 响应 ───│ stopReason=end_turn
  │                 │                    │ <─ 返回 ───────────────────────────│                   │                  │
  │                 │                    │ 组装 {reply: join(reply_chunks),                       │                  │
  │                 │                    │       session_id, thoughts, tool_events}               │                  │
  │ <─ 200 JSON ────│                    │                 │                   │                   │                  │
  │ 渲染气泡+折叠区   │                    │                 │                   │                   │                  │
  │ checkHealth()   │                    │                 │                   │                   │                  │
```

## ③ /api/session/reset（新会话）

```
浏览器            FastAPI            AgentManager         conn                traecli
  │                 │                    │                 │                   │
  │ POST reset ────>│ reset_session() ──>│ async with lock │                   │
  │                 │                    │ alive()? ──否──>│ 懒启动后直接返回    │
  │                 │                    │ ── new_session >│── session/new ──>│ （旧会话仍存在，只是换 id）
  │                 │                    │ <─ 新 session_id ──────────────────│
  │ <─ {session_id}─│                    │                 │                   │
  │ 清空消息区       │                    │                 │                   │
```

## ④ 超时/异常路径

```
浏览器            FastAPI            AgentManager         conn                traecli
  │                 │                    │                 │                   │
  │ POST /api/chat  │                    │ wait_for 超时     │                   │
  │ {timeout?} ────>│                    │ TimeoutError     │                   │
  │                 │                    │ ── cancel(2s) ─>│── session/cancel>│ 中止当前生成
  │ <─ 504 ─────────│ "agent prompt timed out"             │                   │
  │                 │                    │ RequestError ───>│                   │
  │ <─ 502 ─────────│ "agent request error / communication failed"             │
  │ 气泡标红显示错误   │                    │ 子进程死亡: 下次 chat() 自动懒重启(①)  │                   │
```

## 关键源码对照

| 时序图节点 | 源码位置 |
|---|---|
| lifespan 启动 | `trae_http.py` `lifespan()` |
| 子进程 spawn | `trae_http.py` `AgentManager._start_locked()` → `asyncio.create_subprocess_exec` |
| initialize / new_session | `trae_http.py` `AgentManager._start_locked()` |
| 锁串行化 | `trae_http.py` `AgentManager.chat()` / `reset_session()` 的 `async with self.lock` |
| prompt + 超时 | `trae.py` `_send_prompt()`（`asyncio.wait_for`） |
| 超时 cancel + 504 | `trae_http.py` `AgentManager.chat()` except 分支 |
| 流式采集 | `trae_http.py` `CollectingTraeClient.session_update()` |
| 控制台日志（super 委托） | `trae.py` `TraeClient.session_update()` |
| 权限审批 | `trae.py` `TraeClient.request_permission()` |
| 懒重启 | `trae_http.py` `AgentManager._ensure_started_locked()` |
| 关闭清理 | `trae.py` `_shutdown()` |

## 容易忽略的点

1. `session_update` 通知是 traecli **主动推送**的 notification，与 `conn.prompt()` 的最终响应是两条独立消息流。
2. 采集和打印在**同一个调用栈**里：`CollectingTraeClient.session_update` 先采集，再 `super()` 委托给 `TraeClient.session_update` 打印。
3. `/api/session/reset` 是"换新会话"而非"清空旧会话"，旧 session 在 traecli 侧依然占用。
4. 整条链路只有 `traecli → 云端模型` 走网络，其余全部是本机进程内/进程间通信（HTTP 除外，浏览器 → FastAPI 走本机 HTTP）。
