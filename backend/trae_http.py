#!/Users/bytedance/Documents/ByteDance-PreSales/TraeCode-Hands-on/Browser-url-auto-test/ACP_PY_TEST/.venv/bin/python
"""HTTP wrapper that exposes the TRAE CLI ACP client (trae.py) as a REST API.

Run:
    .venv/bin/python trae_http.py [--port 8300] [--model ...] [--yolo]

--yolo: traecli 加 -y（跳过所有确认、无沙箱执行），客户端同时自动批准残留权限请求。

Endpoints:
    GET  /api/health         -> {"status", "connected", "protocol_version", "session_id"}
    POST /api/chat           -> {"message": str, "timeout"?: float} => SSE 流:
         data: {"type": "chunk"|"thought"|"tool", "text": str}   # 实时增量
         data: {"type": "done", "reply", "session_id", "thoughts", "tool_events"}
         data: {"type": "error", "detail": str}
    POST /api/session/reset  -> {"session_id": str}   (start a fresh conversation)

A single traecli ACP process + session is kept alive for all requests, so
consecutive /api/chat calls share the same conversation context. Prompts are
serialized with a lock (one prompt at a time on the shared session).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from acp import PROTOCOL_VERSION, RequestError, connect_to_agent
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ClientCapabilities,
    FileSystemCapabilities,
    Implementation,
    TextContentBlock,
    ToolCallStart,
)

from trae import TraeClient, _resolve_trae_cli, _send_prompt, _shutdown


def _block_text(content: object) -> str:
    """Extract plain text from a content block (single block or dict)."""
    if isinstance(content, TextContentBlock):
        return content.text
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


# traecli 偶尔会把内部提示作为 agent 消息推送（如技能列表被裁剪的说明），过滤掉避免污染回复
_TRAECLI_BANNER_PREFIXES = (
    "Skill descriptions were shortened",
)


class CollectingTraeClient(TraeClient):
    """TraeClient that also records agent output so HTTP handlers can return it.

    All updates are still delegated to ``TraeClient.session_update`` which
    prints them to stdout, so the server console keeps the same logging as
    the interactive client.
    """

    def __init__(self, auto_approve: bool) -> None:
        super().__init__(auto_approve)
        self.reply_chunks: list[str] = []
        self.thought_chunks: list[str] = []
        self.tool_events: list[str] = []
        # SSE 实时事件队列；非 None 时 session_update 会边采集边推送给前端
        self.events: asyncio.Queue | None = None

    def reset(self) -> None:
        self.reply_chunks.clear()
        self.thought_chunks.clear()
        self.tool_events.clear()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        event: dict[str, str] | None = None
        if isinstance(update, AgentMessageChunk):
            text = _block_text(update.content)
            if text and not text.startswith(_TRAECLI_BANNER_PREFIXES):
                self.reply_chunks.append(text)
                event = {"type": "chunk", "text": text}
        elif isinstance(update, AgentThoughtChunk):
            text = _block_text(update.content)
            if text:
                self.thought_chunks.append(text)
                event = {"type": "thought", "text": text}
        elif isinstance(update, ToolCallStart):
            title = f"start: {update.title or update.tool_call_id}"
            self.tool_events.append(title)
            event = {"type": "tool", "text": title}
        if event is not None and self.events is not None:
            await self.events.put(event)
        await super().session_update(session_id, update, **kwargs)


class AgentManager:
    """Owns the traecli ACP subprocess and a single long-lived session."""

    def __init__(
        self,
        trae_bin: str | None,
        model: str | None,
        auto_approve: bool,
        prompt_timeout: float | None,
        debug: bool = False,
    ) -> None:
        self._trae_bin = trae_bin
        self._model = model
        self._auto_approve = auto_approve
        self._prompt_timeout = prompt_timeout
        self._debug = debug
        self.proc: asyncio.subprocess.Process | None = None
        self.conn: Any = None
        self.client: CollectingTraeClient | None = None
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        # One prompt at a time on the shared ACP session.
        self.lock = asyncio.Lock()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self.conn is not None

    async def start(self) -> None:
        async with self.lock:
            if not self.alive():
                await self._start_locked()

    async def stop(self) -> None:
        if self.proc is not None:
            await _shutdown(self.proc, self.conn)
        self.proc = None
        self.conn = None
        self.client = None
        self.session_id = None

    async def reset_session(self) -> str:
        async with self.lock:
            if not self.alive():
                await self._ensure_started_locked()
                assert self.session_id is not None
                return self.session_id
            session = await self.conn.new_session(cwd=os.getcwd(), mcp_servers=[])
            self.session_id = session.session_id
            return self.session_id

    async def chat(self, message: str, timeout: float | None, events: asyncio.Queue | None = None) -> dict[str, Any]:
        async with self.lock:
            if not self.alive():
                # Lazily (re)start traecli if it died or was never started.
                await self._ensure_started_locked()
            assert self.client is not None and self.conn is not None and self.session_id is not None
            self.client.reset()
            self.client.events = events
            try:
                effective_timeout = self._prompt_timeout if timeout is None else (None if timeout <= 0 else timeout)
                await _send_prompt(self.conn, self.session_id, message, effective_timeout)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.conn.cancel(session_id=self.session_id), timeout=2)
                raise HTTPException(status_code=504, detail="agent prompt timed out") from None
            except RequestError as err:
                raise HTTPException(status_code=502, detail=f"agent request error: {err}") from err
            except HTTPException:
                raise
            except Exception as exc:  # broken pipe / dead subprocess, etc.
                raise HTTPException(status_code=502, detail=f"agent communication failed: {exc}") from exc
            finally:
                self.client.events = None
            return {
                "reply": "".join(self.client.reply_chunks),
                "session_id": self.session_id,
                "thoughts": list(self.client.thought_chunks),
                "tool_events": list(self.client.tool_events),
            }

    async def abort_current(self) -> None:
        """Best-effort cancel of the in-flight prompt (used on client disconnect)."""
        if self.conn is not None and self.session_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.conn.cancel(session_id=self.session_id), timeout=2)

    async def _ensure_started_locked(self) -> None:
        try:
            await self._start_locked()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RequestError as err:
            await self.stop()
            raise HTTPException(status_code=503, detail=f"failed to initialize traecli ACP: {err}") from err
        except Exception as exc:
            await self.stop()
            raise HTTPException(status_code=503, detail=f"failed to start traecli: {exc}") from exc

    async def _start_locked(self) -> None:
        await self.stop()  # clean up any previous leftovers
        trae_path = _resolve_trae_cli(self._trae_bin)
        cmd = [trae_path, "acp", "serve"]
        if self._model:
            cmd += ["-c", f'model="{self._model}"']
        if self._auto_approve:
            # traecli 端跳过所有确认提示（无沙箱直接执行），权限请求不再产生
            cmd += ["-y"]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None if self._debug else asyncio.subprocess.DEVNULL,
        )
        self.client = CollectingTraeClient(auto_approve=self._auto_approve)
        self.conn = connect_to_agent(self.client, self.proc.stdin, self.proc.stdout)
        init_resp = await self.conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(
                fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                terminal=True,
            ),
            client_info=Implementation(name="trae-http-client", title="TRAE HTTP Client", version="0.1.0"),
        )
        self.protocol_version = init_resp.protocol_version
        session = await self.conn.new_session(cwd=os.getcwd(), mcp_servers=[])
        self.session_id = session.session_id


# Module-level default so the app is importable; main() rebinds it with CLI args.
manager: AgentManager = AgentManager(
    trae_bin=None, model=None, auto_approve=False, prompt_timeout=120.0
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best effort: bring traecli up with the server; /api/chat retries on demand.
    with contextlib.suppress(Exception):
        await manager.start()
    yield
    with contextlib.suppress(Exception):
        await manager.stop()


app = FastAPI(title="TRAE CLI ACP HTTP API", lifespan=lifespan)

# 前后端分离：允许本地前端跨域访问（与 backend/main.py 一致）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    timeout: float | None = None  # seconds; <=0 disables the timeout


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "connected": manager.alive(),
        "protocol_version": manager.protocol_version,
        "session_id": manager.session_id,
    }


async def _run_chat(message: str, timeout: float | None, queue: asyncio.Queue) -> None:
    """Run the prompt in the background; push a terminal done/error event at the end."""
    try:
        result = await manager.chat(message, timeout, events=queue)
    except HTTPException as exc:
        await queue.put({"type": "error", "detail": str(exc.detail)})
        return
    except Exception as exc:  # noqa: BLE001
        await queue.put({"type": "error", "detail": f"agent communication failed: {exc}"})
        return
    await queue.put({"type": "done", **result})


async def _chat_sse(queue: asyncio.Queue, task: asyncio.Task) -> AsyncIterator[str]:
    """Drain live agent events as Server-Sent Events until done/error."""
    try:
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event.get("type") in ("done", "error"):
                return
    finally:
        # 客户端断开（刷新/关页）：取消后台 prompt 并尽力通知 traecli 停止生成
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            await manager.abort_current()


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_run_chat(req.message, req.timeout, queue))
    return StreamingResponse(_chat_sse(queue, task), media_type="text/event-stream")


@app.post("/api/session/reset")
async def reset_session() -> dict[str, str]:
    return {"session_id": await manager.reset_session()}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv[1:]
    parser = argparse.ArgumentParser(description="Expose the TRAE CLI ACP client as an HTTP API.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8300, help="Bind port (default: 8300)")
    parser.add_argument("--trae", help="Path to the traecli binary")
    parser.add_argument("--model", help='Model identifier (passed to traecli as -c model="...")')
    parser.add_argument(
        "--prompt-timeout",
        type=float,
        default=120.0,
        help="Default seconds to wait for a prompt to finish; 0 disables",
    )
    parser.add_argument("--debug", action="store_true", help="Show traecli stderr output")
    parser.add_argument(
        "--yolo",
        action="store_true",
        help="Pass -y to traecli (skip all confirmations, no sandbox) and auto-approve leftover permission prompts",
    )
    ns = parser.parse_args(args)

    global manager
    manager = AgentManager(
        trae_bin=ns.trae,
        model=ns.model,
        auto_approve=ns.yolo,
        prompt_timeout=None if ns.prompt_timeout == 0 else ns.prompt_timeout,
        debug=ns.debug,
    )

    uvicorn.run(app, host=ns.host, port=ns.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
