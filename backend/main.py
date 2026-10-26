"""Chat Agent 后端骨架（逻辑暂未实现，仅提供占位接口）。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Chat Agent API")

# 前后端分离：允许本地前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    # TODO: 在这里接入真正的 Agent / LLM 逻辑
    return ChatResponse(reply=f"[Mock 回复] 你说的是：{req.message}")
