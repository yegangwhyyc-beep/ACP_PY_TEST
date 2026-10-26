// ===== 配置 =====
const API_BASE = "http://localhost:8000"; // 后端地址

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const statusEl = document.getElementById("api-status");

// ===== 后端可用性检测 =====
(async function checkApi() {
  try {
    const res = await fetch(`${API_BASE}/api/health`);
    if (!res.ok) throw new Error();
    statusEl.textContent = "后端已连接（Mock）";
    statusEl.classList.add("online");
  } catch {
    statusEl.textContent = "后端未启动 · 本地 Mock 模式";
    statusEl.classList.add("offline");
  }
})();

// ===== 渲染消息 =====
function addMessage(text, role) {
  const div = document.createElement("div");
  div.className = `message ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  div.appendChild(bubble);
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

function addTyping() {
  const bubble = addMessage("正在输入…", "assistant");
  bubble.classList.add("typing");
  return bubble;
}

// ===== 发送逻辑 =====
async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text) return;

  addMessage(text, "user");
  inputEl.value = "";
  sendBtn.disabled = true;

  const typingBubble = addTyping();
  try {
    const reply = await requestChat(text);
    typingBubble.textContent = reply;
  } catch {
    typingBubble.textContent = "[本地 Mock 回复] 你说的是：" + text;
  } finally {
    typingBubble.classList.remove("typing");
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

async function requestChat(message) {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return data.reply;
}

// ===== 事件绑定 =====
sendBtn.addEventListener("click", sendMessage);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendMessage();
});
