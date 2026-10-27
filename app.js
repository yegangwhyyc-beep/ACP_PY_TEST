"use strict";

// ===== 配置 =====
const DEFAULT_BASE = "http://127.0.0.1:8300";
const apiBaseInput = document.getElementById("api-base");
const messagesEl  = document.getElementById("messages");
const inputEl     = document.getElementById("input");
const sendBtn     = document.getElementById("send-btn");
const resetBtn    = document.getElementById("reset-btn");
const statusEl    = document.getElementById("status");
const dotEl       = document.getElementById("dot");

// API 地址可改，持久化到 localStorage
apiBaseInput.value = localStorage.getItem("trae_api_base") || DEFAULT_BASE;
let API_BASE = apiBaseInput.value.replace(/\/+$/, "");
apiBaseInput.addEventListener("change", () => {
  API_BASE = apiBaseInput.value.trim().replace(/\/+$/, "");
  localStorage.setItem("trae_api_base", API_BASE);
  checkHealth();
});

// ===== 健康检查：GET /api/health =====
async function checkHealth() {
  try {
    const res = await fetch(`${API_BASE}/api/health`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const h = await res.json();
    dotEl.className = "dot " + (h.connected ? "online" : "offline");
    statusEl.innerHTML = h.connected
      ? `已连接 traecli · 协议 v${h.protocol_version}<br/><code>会话 ${short(h.session_id)}</code>`
      : `服务在线，但 traecli 未连接`;
  } catch {
    dotEl.className = "dot offline";
    statusEl.textContent = "服务不可达（先启动 trae_http.py）";
  }
}
function short(id) { return id ? id.slice(0, 13) + "…" : "-"; }
checkHealth();
setInterval(checkHealth, 15000);

// ===== 渲染 =====
function addBubble(text, role, cls) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = `bubble${cls ? " " + cls : ""}`;
  bubble.textContent = text;
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

function addExtras(bubble, thoughts, toolEvents) {
  if ((!thoughts || !thoughts.length) && (!toolEvents || !toolEvents.length)) return;
  const details = document.createElement("details");
  details.className = "extra";
  const parts = [];
  if (thoughts && thoughts.length) parts.push(`思考过程 (${thoughts.length})`);
  if (toolEvents && toolEvents.length) parts.push(`工具调用 (${toolEvents.length})`);
  const summary = document.createElement("summary");
  summary.textContent = parts.join(" · ");
  const box = document.createElement("div");
  box.className = "items";
  (thoughts || []).forEach(t => { const d = document.createElement("div"); d.textContent = "💭 " + t; box.appendChild(d); });
  (toolEvents || []).forEach(t => { const d = document.createElement("div"); d.textContent = "🔧 " + t; box.appendChild(d); });
  details.appendChild(summary);
  details.appendChild(box);
  bubble.appendChild(details);
  bubble.style.whiteSpace = "normal";
}

// ===== 发送：POST /api/chat =====
let busy = false;
async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || busy) return;
  busy = true;
  sendBtn.disabled = true;
  inputEl.value = "";
  addBubble(text, "user");

  const typing = addBubble("思考中…", "assistant", "typing");
  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    typing.classList.remove("typing");
    typing.textContent = data.reply || "(空回复)";
    addExtras(typing, data.thoughts, data.tool_events);
  } catch (err) {
    typing.classList.remove("typing");
    typing.classList.add("error");
    typing.textContent = "请求失败：" + err.message;
  } finally {
    busy = false;
    sendBtn.disabled = false;
    inputEl.focus();
    messagesEl.scrollTop = messagesEl.scrollHeight;
    checkHealth();
  }
}

// ===== 新会话：POST /api/session/reset =====
resetBtn.addEventListener("click", async () => {
  if (busy) return;
  try {
    const res = await fetch(`${API_BASE}/api/session/reset`, { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    messagesEl.innerHTML = "";
    addBubble("已开启新会话，上下文已清空。", "assistant");
    checkHealth();
  } catch (err) {
    addBubble("新会话创建失败：" + err.message, "assistant", "error");
  }
});

sendBtn.addEventListener("click", sendMessage);
inputEl.addEventListener("keydown", e => { if (e.key === "Enter") sendMessage(); });

addBubble("你好，我是 TRAE Agent（通过 traecli ACP 接入）。确保 trae_http.py 已在运行，然后随便聊点什么吧。", "assistant");
