from acp import connect_to_agent
from acp.http import create_http_stream
from acp.ws import create_websocket_stream

# Streamable HTTP
transport = create_http_stream("http://localhost:8000/acp")
conn = connect_to_agent(my_client, transport)

# 
#transport = await create_websocket_stream("ws://localhost:8000/acp")
#conn = connect_to_agent(my_client, transport)

# 由于await关键字仅允许在异步函数内部使用，将初始化操作包裹在异步函数中执行
async def initialize_connection():
    init = await conn.initialize(protocol_version=1)
    return init

# 将所有使用await的代码都包裹在异步main函数中，避免在顶层直接使用await语法错误
async def main():
    init = await initialize_connection()
    session = await conn.new_session(cwd="/tmp", mcp_servers=[])
    await conn.prompt(session_id=session.session_id, prompt=[text_block("你好！")])
    await conn.close()
    await transport.close()
# 此处await操作已移入异步main函数内部，符合Python语法规范，不允许在顶层直接使用await关键字
# 顶层作用域不允许直接使用await，该操作已移入main异步函数内部执行
# 该await操作已移入main异步函数内部执行，顶层作用域不允许直接使用await关键字
# 顶层作用域不允许直接使用await，该操作已移入main异步函数内部执行
# 移除顶层的非法await调用，transport关闭操作已在main异步函数内部正确执行
