from __future__ import annotations

import argparse
import asyncio
import asyncio.subprocess
import contextlib
import json
import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from acp import (
    PROTOCOL_VERSION,
    Client,
    RequestError,
    connect_to_agent,
)
from acp.core import ClientSideConnection
from acp.schema import (
    AgentMessageChunk,
    AgentPlanContentUpdate,
    AgentPlanRemovedUpdate,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    ClientCapabilities,
    ConfigOptionUpdate,
    CreateElicitationResponse,
    CreateTerminalResponse,
    CurrentModeUpdate,
    DeclineElicitationResponse,
    DeniedOutcome,
    ElicitationMode,
    EmbeddedResourceContentBlock,
    EnvVariable,
    FileEditToolCallContent,
    FileSystemCapabilities,
    Implementation,
    KillTerminalResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    ResourceContentBlock,
    SessionInfoUpdate,
    TerminalOutputResponse,
    TerminalToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)


class TraeClient(Client):
    """Minimal client implementation that can drive the TRAE CLI over ACP."""

    def __init__(self, auto_approve: bool) -> None:
        self._auto_approve = auto_approve

    async def request_permission(
        self, session_id: str, tool_call: ToolCallUpdate, options: list[PermissionOption], **kwargs: Any
    ) -> RequestPermissionResponse:
        if self._auto_approve:
            option = _pick_preferred_option(options)
            if option is None:
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
            return RequestPermissionResponse(outcome=AllowedOutcome(option_id=option.option_id, outcome="selected"))

        title = tool_call.title or "<permission>"
        if not options:
            print(f"\n🔐 Permission requested: {title} (no options, cancelling)")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        print(f"\n🔐 Permission requested: {title}")
        for idx, opt in enumerate(options, start=1):
            print(f"  {idx}. {opt.name} ({opt.kind})")

        loop = asyncio.get_running_loop()
        while True:
            choice = await loop.run_in_executor(None, lambda: input("Select option: ").strip())
            if not choice:
                continue
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(options):
                    opt = options[idx]
                    return RequestPermissionResponse(
                        outcome=AllowedOutcome(option_id=opt.option_id, outcome="selected")
                    )
            print("Invalid selection, try again.")

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> WriteTextFileResponse | None:
        pathlib_path = Path(path)
        if not pathlib_path.is_absolute():
            raise RequestError.invalid_params({"path": pathlib_path, "reason": "path must be absolute"})
        pathlib_path.parent.mkdir(parents=True, exist_ok=True)
        pathlib_path.write_text(content)
        print(f"[Client] Wrote {pathlib_path} ({len(content)} bytes)")
        return WriteTextFileResponse()

    async def read_text_file(
        self, session_id: str, path: str, line: int | None = None, limit: int | None = None, **kwargs: Any
    ) -> ReadTextFileResponse:
        pathlib_path = Path(path)
        if not pathlib_path.is_absolute():
            raise RequestError.invalid_params({"path": pathlib_path, "reason": "path must be absolute"})
        text = pathlib_path.read_text()
        print(f"[Client] Read {pathlib_path} ({len(text)} bytes)")
        if line is not None or limit is not None:
            text = _slice_text(text, line, limit)
        return ReadTextFileResponse(content=text)

    async def session_update(  # noqa: C901
        self,
        session_id: str,
        update: UserMessageChunk
        | AgentMessageChunk
        | AgentThoughtChunk
        | ToolCallStart
        | ToolCallProgress
        | AgentPlanUpdate
        | AgentPlanContentUpdate
        | AgentPlanRemovedUpdate
        | AvailableCommandsUpdate
        | CurrentModeUpdate
        | ConfigOptionUpdate
        | SessionInfoUpdate
        | UsageUpdate,
        **kwargs: Any,
    ) -> None:
        if isinstance(update, AgentMessageChunk):
            _print_text_content(update.content)
        elif isinstance(update, AgentThoughtChunk):
            print("\n[agent_thought]")
            _print_text_content(update.content)
        elif isinstance(update, UserMessageChunk):
            print("\n[user_message]")
            _print_text_content(update.content)
        elif isinstance(update, AgentPlanUpdate):
            print("\n[plan]")
            for entry in update.entries:
                print(f" - {entry.status.upper():<10} {entry.content}")
        elif isinstance(update, AgentPlanContentUpdate):
            print(f"\n[plan update] {update.plan.id}")
        elif isinstance(update, AgentPlanRemovedUpdate):
            print(f"\n[plan removed] {update.id}")
        elif isinstance(update, AvailableCommandsUpdate):
            # TRAE CLI pushes a large command/skill list; print names only.
            names = ", ".join(command.name for command in update.available_commands)
            print(f"\n[available commands] {names}")
        elif isinstance(update, ToolCallStart):
            print(f"\n🔧 {update.title} ({update.status or 'pending'})")
        elif isinstance(update, ToolCallProgress):
            status = update.status or "in_progress"
            print(f"\n🔧 Tool call `{update.tool_call_id}` → {status}")
            if update.content:
                for item in update.content:
                    if isinstance(item, FileEditToolCallContent):
                        print(f"  diff: {item.path}")
                    elif isinstance(item, TerminalToolCallContent):
                        print(f"  terminal: {item.terminal_id}")
                    elif isinstance(item, dict):
                        print(f"  content: {json.dumps(item, indent=2)}")
        else:
            print(f"\n[session update] {update}")

    # Optional / terminal-related methods ---------------------------------
    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        print(f"[Client] createTerminal: {command} {args or []} (cwd={cwd})")
        return CreateTerminalResponse(terminal_id="term-1")

    async def create_elicitation(self, message: str, mode: ElicitationMode, **kwargs: Any) -> CreateElicitationResponse:
        print(f"\n[elicitation] {message} ({type(mode).__name__})")
        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        print(f"\n[elicitation complete] {elicitation_id}")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse:
        print(f"[Client] terminalOutput: {session_id} {terminal_id}")
        return TerminalOutputResponse(output="", truncated=False)

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        print(f"[Client] releaseTerminal: {session_id} {terminal_id}")
        return ReleaseTerminalResponse()

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        print(f"[Client] waitForTerminalExit: {session_id} {terminal_id}")
        return WaitForTerminalExitResponse()

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> KillTerminalResponse | None:
        print(f"[Client] killTerminal: {session_id} {terminal_id}")
        return KillTerminalResponse()


def _pick_preferred_option(options: Iterable[PermissionOption]) -> PermissionOption | None:
    best: PermissionOption | None = None
    for option in options:
        if option.kind in {"allow_once", "allow_always"}:
            return option
        best = best or option
    return best


def _slice_text(content: str, line: int | None, limit: int | None) -> str:
    lines = content.splitlines()
    start = 0
    if line:
        start = max(line - 1, 0)
    end = len(lines)
    if limit:
        end = min(start + limit, end)
    return "\n".join(lines[start:end])


def _print_text_content(content: object) -> None:
    if isinstance(content, TextContentBlock):
        print(content.text)
    elif isinstance(content, ResourceContentBlock):
        print(f"{content.name or content.uri}")
    elif isinstance(content, EmbeddedResourceContentBlock):
        resource = content.resource
        text = getattr(resource, "text", None)
        if text:
            print(text)
        else:
            blob = getattr(resource, "blob", None)
            print(blob if blob else "<embedded resource>")
    elif isinstance(content, dict):
        text = content.get("text")  # type: ignore[union-attr]
        if text:
            print(text)


async def _send_prompt(conn: ClientSideConnection, session_id: str, prompt: str, timeout: float | None) -> None:
    request = conn.prompt(
        session_id=session_id,
        prompt=[TextContentBlock(type="text", text=prompt)],
    )
    if timeout is None:
        await request
        return
    await asyncio.wait_for(request, timeout=timeout)


async def interactive_loop(conn: ClientSideConnection, session_id: str, prompt_timeout: float | None) -> None:
    print("Type a message and press Enter to send.")
    print("Commands: :cancel, :exit")

    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("\n> ").strip())
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not line:
            continue
        if line in {":exit", ":quit"}:
            break
        if line == ":cancel":
            await conn.cancel(session_id=session_id)
            continue

        try:
            await _send_prompt(conn, session_id, line, prompt_timeout)
        except asyncio.TimeoutError:
            print("prompt timed out waiting for final ACP response", file=sys.stderr)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.cancel(session_id=session_id), timeout=2)
        except RequestError as err:
            _print_request_error("prompt", err)
        except Exception as exc:
            print(f"Prompt failed: {exc}", file=sys.stderr)


def _resolve_trae_cli(binary: str | None) -> str:
    if binary:
        return binary
    env_value = os.environ.get("ACP_TRAE_BIN")
    if env_value:
        return env_value
    resolved = shutil.which("traecli")
    if resolved:
        return resolved
    raise FileNotFoundError("Unable to locate `traecli` CLI, provide --trae path")


async def run(argv: list[str]) -> int:  # noqa: C901
    parser = argparse.ArgumentParser(description="Interact with the TRAE CLI over ACP.")
    parser.add_argument("--trae", help="Path to the traecli binary")
    parser.add_argument("--model", help='Model identifier (passed to traecli as -c model="...")')
    parser.add_argument("--prompt", help="Send one prompt and exit")
    parser.add_argument(
        "--prompt-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for session/prompt to finish; use 0 to disable",
    )
    parser.add_argument(
        "--allowed-tool",
        action="append",
        help="Allow a tool for this ACP session (repeatable, e.g. Bash(ls:*))",
    )
    parser.add_argument(
        "--disallowed-tool",
        action="append",
        help="Disallow a tool for this ACP session (repeatable)",
    )
    parser.add_argument(
        "--worktree",
        nargs="?",
        const="",
        default=None,
        help="Start in a fresh git worktree (optionally provide a name)",
    )
    parser.add_argument("--debug", action="store_true", help="Show traecli stderr output")
    parser.add_argument("--yolo", action="store_true", help="Auto-approve permission prompts")
    args = parser.parse_args(argv[1:])
    prompt_timeout = None if args.prompt_timeout == 0 else args.prompt_timeout

    try:
        trae_path = _resolve_trae_cli(args.trae)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    cmd = [trae_path, "acp", "serve"]
    if args.model:
        cmd += ["-c", f'model="{args.model}"']
    for tool in args.allowed_tool or []:
        cmd += ["--allowed-tool", tool]
    for tool in args.disallowed_tool or []:
        cmd += ["--disallowed-tool", tool]
    if args.worktree is not None:
        cmd.append("--worktree" if args.worktree == "" else f"--worktree={args.worktree}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Suppress TRAE CLI diagnostic noise unless debugging
            stderr=None if args.debug else asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        print(f"Failed to start TRAE CLI: {exc}", file=sys.stderr)
        return 1

    if proc.stdin is None or proc.stdout is None:
        print("TRAE CLI process did not expose stdio pipes.", file=sys.stderr)
        proc.terminate()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        return 1

    client_impl = TraeClient(auto_approve=args.yolo)
    conn = connect_to_agent(client_impl, proc.stdin, proc.stdout)

    try:
        init_resp = await conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(
                fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                terminal=True,
            ),
            client_info=Implementation(name="trae-acp-client", title="TRAE ACP Client", version="0.1.0"),
        )
    except RequestError as err:
        _print_request_error("initialize", err)
        await _shutdown(proc, conn)
        return 1
    except Exception as exc:
        print(f"initialize error: {exc}", file=sys.stderr)
        await _shutdown(proc, conn)
        return 1

    print(f"✅ Connected to TRAE CLI (protocol v{init_resp.protocol_version})")

    try:
        session = await conn.new_session(
            cwd=os.getcwd(),
            mcp_servers=[],
        )
    except RequestError as err:
        _print_request_error("new_session", err)
        await _shutdown(proc, conn)
        return 1
    except Exception as exc:
        print(f"new_session error: {exc}", file=sys.stderr)
        await _shutdown(proc, conn)
        return 1

    print(f"📝 Created session: {session.session_id}")

    try:
        if args.prompt is None:
            await interactive_loop(conn, session.session_id, prompt_timeout)
        else:
            await _send_prompt(conn, session.session_id, args.prompt, prompt_timeout)
    except asyncio.TimeoutError:
        print("prompt timed out waiting for final ACP response", file=sys.stderr)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.cancel(session_id=session.session_id), timeout=2)
        return 1
    finally:
        await _shutdown(proc, conn)

    return 0


def _print_request_error(stage: str, err: RequestError) -> None:
    payload = err.to_error_obj()
    message = payload.get("message", "")
    code = payload.get("code")
    print(f"{stage} error ({code}): {message}", file=sys.stderr)
    data = payload.get("data")
    if data is not None:
        try:
            formatted = json.dumps(data, indent=2)
        except TypeError:
            formatted = str(data)
        print(formatted, file=sys.stderr)


async def _shutdown(proc: asyncio.subprocess.Process, conn: ClientSideConnection) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(conn.close(), timeout=2)
    if proc.stdin is not None:
        with contextlib.suppress(Exception):
            proc.stdin.close()
            await asyncio.wait_for(proc.stdin.wait_closed(), timeout=2)
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    try:
        return asyncio.run(run(list(args)))
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
