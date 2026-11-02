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

// ===== Markdown 渲染（助手回复） =====
if (typeof marked !== "undefined") {
  marked.use({ gfm: true, breaks: true });
}
function renderMarkdown(text) {
  const tpl = document.createElement("template");
  if (typeof marked !== "undefined") {
    tpl.innerHTML = marked.parse(text);
  } else {
    // CDN 不可用时的降级：转义 HTML 并保留换行
    const esc = document.createElement("div");
    esc.textContent = text;
    tpl.innerHTML = esc.innerHTML;
  }
  // 剥离可执行内容，防止回复中的脚本/事件处理器注入页面
  tpl.content.querySelectorAll("script, style, iframe, object, embed").forEach(el => el.remove());
  tpl.content.querySelectorAll("*").forEach(el => {
    for (const attr of [...el.attributes]) {
      if (/^on/i.test(attr.name) || (attr.name === "href" && /^\s*javascript:/i.test(attr.value))) {
        el.removeAttribute(attr.name);
      }
    }
  });
  return tpl.innerHTML;
}
function fillMarkdown(bubble, text) {
  bubble.classList.add("md");
  bubble.innerHTML = renderMarkdown(text);
  if (typeof hljs !== "undefined") {
    bubble.querySelectorAll("pre code").forEach(el => { try { hljs.highlightElement(el); } catch {} });
  }
}

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

// ===== 实时事件区（思考/工具调用），流式期间持续追加 =====
function createExtras(bubble) {
  const details = document.createElement("details");
  details.className = "extra";
  details.style.display = "none";
  const summary = document.createElement("summary");
  const box = document.createElement("div");
  box.className = "items";
  details.appendChild(summary);
  details.appendChild(box);
  bubble.appendChild(details);
  bubble.style.whiteSpace = "normal";
  const counts = { thought: 0, tool: 0 };
  return {
    add(kind, text) {
      details.style.display = "";
      counts[kind]++;
      const parts = [];
      if (counts.thought) parts.push(`思考过程 (${counts.thought})`);
      if (counts.tool) parts.push(`工具调用 (${counts.tool})`);
      summary.textContent = parts.join(" · ");
      const d = document.createElement("div");
      d.textContent = (kind === "thought" ? "💭 " : "🔧 ") + text;
      box.appendChild(d);
      box.scrollTop = box.scrollHeight;
    },
  };
}

// ===== 发送：POST /api/chat（SSE 流式） =====
let busy = false;
async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || busy) return;
  busy = true;
  sendBtn.disabled = true;
  inputEl.value = "";
  addBubble(text, "user");

  const bubble = addBubble("思考中…", "assistant", "typing");
  const placeholder = bubble.firstChild; // “思考中…” 文本节点
  const content = document.createElement("div"); // markdown 渲染目标，与事件区隔离
  bubble.appendChild(content);
  const extras = createExtras(bubble);
  let replyText = "";
  let rendered = false;

  const render = () => {
    if (!replyText) return;
    rendered = true;
    if (placeholder) placeholder.remove();
    bubble.classList.add("md");
    fillMarkdown(content, replyText);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  };

  const handleEvent = ev => {
    if (ev.type === "chunk") {
      replyText += ev.text;
      render();
    } else if (ev.type === "thought" || ev.type === "tool") {
      extras.add(ev.type, ev.text);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    } else if (ev.type === "done") {
      replyText = ev.reply || replyText;
      render();
    } else if (ev.type === "error") {
      throw new Error(ev.detail || "unknown error");
    }
  };

  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // SSE 长连接：关闭服务端超时（0），需要中止时刷新页面即可（断连会取消 prompt）
      body: JSON.stringify({ message: text, timeout: 0 }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { const e = await res.json(); if (e.detail) detail = e.detail; } catch {}
      throw new Error(detail);
    }
    if (!res.body) throw new Error("当前浏览器不支持流式读取");

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        const dataLine = frame.split("\n").find(l => l.startsWith("data:"));
        if (!dataLine) continue;
        handleEvent(JSON.parse(dataLine.slice(5).trim()));
      }
    }
    if (!rendered) {
      if (placeholder) placeholder.remove();
      content.textContent = replyText || "(空回复)";
    }
  } catch (err) {
    if (!rendered && placeholder) placeholder.remove();
    bubble.classList.add("error");
    const d = document.createElement("div");
    d.textContent = "请求失败：" + (err.message || "未知错误");
    bubble.appendChild(d);
  } finally {
    bubble.classList.remove("typing");
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
