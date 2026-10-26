#!/Users/bytedance/Documents/ByteDance-PreSales/TraeCode-Hands-on/Browser-url-auto-test/ACP_PY_TEST/.venv/bin/python
"""HTTP wrapper that exposes the TRAE CLI ACP client (trae.py) as a REST API.

Run:
    .venv/bin/python trae_http.py [--port 8300] [--model ...] [--yolo]

Endpoints:
    GET  /api/health         -> {"status", "connected", "protocol_version", "session_id"}
    POST /api/chat           -> {"message": str, "timeout"?: float} => {"reply", "session_id"}
    POST /api/session/reset  -> {"session_id": str}   (start a fresh conversation)

A single traecli ACP process + session is kept alive for all requests, so
consecutive /api/chat calls share the same conversation context. Prompts are
serialized with a lock (one prompt at a time on the shared session).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

    def reset(self) -> None:
        self.reply_chunks.clear()
        self.thought_chunks.clear()
        self.tool_events.clear()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if isinstance(update, AgentMessageChunk):
            text = _block_text(update.content)
            if text and not text.startswith(_TRAECLI_BANNER_PREFIXES):
                self.reply_chunks.append(text)
        elif isinstance(update, AgentThoughtChunk):
            text = _block_text(update.content)
            if text:
                self.thought_chunks.append(text)
        elif isinstance(update, ToolCallStart):
            self.tool_events.append(f"start: {update.title or update.tool_call_id}")
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

    async def chat(self, message: str, timeout: float | None) -> dict[str, Any]:
        async with self.lock:
            if not self.alive():
                # Lazily (re)start traecli if it died or was never started.
                await self._ensure_started_locked()
            assert self.client is not None and self.conn is not None and self.session_id is not None
            self.client.reset()
            effective_timeout = self._prompt_timeout if timeout is None else (None if timeout <= 0 else timeout)
            try:
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
            return {
                "reply": "".join(self.client.reply_chunks),
                "session_id": self.session_id,
                "thoughts": list(self.client.thought_chunks),
                "tool_events": list(self.client.tool_events),
            }

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


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    thoughts: list[str]
    tool_events: list[str]


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "connected": manager.alive(),
        "protocol_version": manager.protocol_version,
        "session_id": manager.session_id,
    }


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    result = await manager.chat(req.message, req.timeout)
    return ChatResponse(**result)


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
    parser.add_argument("--yolo", action="store_true", help="Auto-approve permission prompts")
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
