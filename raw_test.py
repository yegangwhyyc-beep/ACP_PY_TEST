"""原始 JSON-RPC stdio 测试：不走 acp 客户端库，直接向 echo_agent 发 NDJSON 并打印全部输出。"""

import asyncio
import json
import sys


async def main() -> None:
    command = sys.argv[1:] or [sys.executable, "frontend/echo_agent.py"]
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout

    async def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj).encode() + b"\n")
        await proc.stdin.drain()

    async def read_all(timeout: float) -> list[str]:
        lines: list[str] = []
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout)
                if not line:
                    break
                lines.append(line.decode().strip())
                timeout = 1.0  # 后续行只等 1s
        except asyncio.TimeoutError:
            pass
        return lines

    await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
    print("<< initialize:", await read_all(3))

    await send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": "/tmp", "mcpServers": []},
        }
    )
    lines = await read_all(3)
    print("<< session/new:", lines)
    session_id = json.loads(lines[-1])["result"]["sessionId"] if lines else "?"

    await send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "hello raw"}]},
        }
    )
    print("<< session/prompt:", await read_all(8))

    proc.stdin.close()
    await proc.wait()
    if proc.stderr:
        stderr = await proc.stderr.read()
        if stderr:
            print("== agent stderr ==")
            print(stderr.decode())


if __name__ == "__main__":
    asyncio.run(main())
