"""最小 ACP 客户端：拉起 echo_agent.py 子进程并走完 initialize -> new_session -> prompt 流程。"""

import asyncio
import sys
from typing import Any

from acp.schema import (
    AgentMessageChunk,
    ClientCapabilities,
    Implementation,
    TextContentBlock,
)
from acp.stdio import spawn_agent_process


class PrintClient:
    """实现 Client 协议：收到 agent 消息时打印到终端。"""

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if isinstance(update, AgentMessageChunk):
            print(f"[agent 回复] {update.content.text}")

    async def request_permission(self, session_id: str, tool_call: Any, options: Any, **kwargs: Any) -> None:
        return None  # Echo Agent 不会调用

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any) -> None:
        return None


async def main() -> None:
    async with spawn_agent_process(
        PrintClient(),
        sys.executable,
        "frontend/echo_agent.py",
    ) as (conn, _proc):
        init = await conn.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="test-client", version="0.1.0"),
        )
        print(f"[initialize] protocol_version={init.protocol_version}")

        session = await conn.new_session(cwd=".")
        print(f"[new_session] session_id={session.session_id}")

        try:
            resp = await conn.prompt(
                session_id=session.session_id,
                prompt=[TextContentBlock(type="text", text="你好，ACP！")],
            )
            print(f"[prompt] stop_reason={resp.stop_reason}")
        except Exception as exc:
            print(f"[prompt 失败] {exc}")
            data = getattr(exc, "data", None)
            if data:
                print(f"[错误详情] {data}")
            raise


if __name__ == "__main__":
    asyncio.run(main())
