(() => {
  "use strict";

  const page = document.querySelector('[data-view="chat"]');
  if (!page) return;

  const $ = (selector) => document.querySelector(selector);
  const sessionKey = "cleanllm-chat-session-v1";
  const modelKey = "cleanllm-chat-model";
  const state = {
    messages: [],
    controller: null,
    requestId: "",
  };

  const errorMessage = (data, fallback = "请求失败") => {
    if (data?.error?.message) return String(data.error.message);
    if (Array.isArray(data?.detail)) return data.detail.map((item) => item.msg || String(item)).join("；");
    return String(data?.detail || fallback);
  };

  async function adminRequest(url, options) {
    const response = await fetch(url, options);
    if (response.status === 401) {
      location.replace("/login");
      throw new Error("登录已过期");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorMessage(data));
    return data;
  }

  function notify(message, failed = false) {
    if (typeof toast === "function") toast(message, failed);
  }

  async function copyText(value) {
    try {
      await navigator.clipboard.writeText(value);
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = value;
      area.style.cssText = "position:fixed;opacity:0;pointer-events:none";
      document.body.append(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      if (!copied) throw new Error("copy failed");
    }
  }

  function loadSession() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(sessionKey) || "{}");
      state.messages = Array.isArray(saved.messages)
        ? saved.messages.filter((item) => ["user", "assistant"].includes(item?.role) && typeof item.content === "string").slice(-30)
        : [];
      $("#chat-system-prompt").value = typeof saved.systemPrompt === "string" ? saved.systemPrompt : "";
      $("#chat-stream").checked = saved.stream !== false;
    } catch (_) {
      state.messages = [];
    }
  }

  function saveSession() {
    try {
      sessionStorage.setItem(sessionKey, JSON.stringify({
        messages: state.messages.slice(-30),
        systemPrompt: $("#chat-system-prompt").value,
        stream: $("#chat-stream").checked,
      }));
    } catch (_) {
      // The conversation remains usable when browser storage is unavailable.
    }
  }

  function renderMessages() {
    const container = $("#chat-messages");
    container.replaceChildren();
    if (!state.messages.length) {
      const empty = document.createElement("div");
      empty.className = "chat-empty";
      empty.innerHTML = '<svg><use href="#i-chat"/></svg><h3>开始一次真实链路测试</h3><p>回答会经过当前路由、熔断与响应清洗；CleanLLM 不会把聊天正文写入服务器。</p>';
      container.append(empty);
      return;
    }
    state.messages.forEach((message, index) => {
      const item = document.createElement("article");
      item.className = `chat-message ${message.role}`;
      const head = document.createElement("header");
      const label = document.createElement("strong");
      label.textContent = message.role === "user" ? "你" : "CleanLLM";
      head.append(label);
      if (message.role === "assistant" && message.content) {
        const copy = document.createElement("button");
        copy.type = "button";
        copy.className = "chat-copy icon-button";
        copy.dataset.copyMessage = String(index);
        copy.title = "复制回答";
        copy.setAttribute("aria-label", "复制回答");
        copy.innerHTML = '<svg><use href="#i-copy"/></svg>';
        head.append(copy);
      }
      const content = document.createElement("div");
      content.className = "chat-message-content";
      content.textContent = message.content || "正在等待模型响应…";
      if (!message.content) content.classList.add("pending");
      item.append(head, content);
      container.append(item);
    });
    container.scrollTop = container.scrollHeight;
  }

  function setBusy(busy) {
    $("#chat-send").disabled = busy;
    $("#chat-model").disabled = busy;
    $("#chat-stop").hidden = !busy;
    $("#chat-state").textContent = busy ? "生成中" : "就绪";
    $("#chat-state").classList.toggle("chat-running", busy);
  }

  function setAssistantText(index, text) {
    state.messages[index].content = text;
    const message = $("#chat-messages").children[index];
    const content = message?.querySelector(".chat-message-content");
    if (content) {
      content.textContent = text || "正在等待模型响应…";
      content.classList.toggle("pending", !text);
      $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
    }
  }

  async function loadModels() {
    const select = $("#chat-model");
    const [modelResult, settingsResult] = await Promise.allSettled([
      adminRequest("/api/models"),
      adminRequest("/api/settings"),
    ]);
    const records = modelResult.status === "fulfilled" ? modelResult.value.data || [] : [];
    const virtualModels = settingsResult.status === "fulfilled" ? settingsResult.value.virtual_models || [] : [];
    const choices = new Map();
    virtualModels.forEach((item) => {
      const id = String(item?.alias || "").trim();
      if (id) choices.set(id, { id, label: `${id} · 虚拟模型`, virtual: true });
    });
    records.forEach((item) => {
      const id = String(item?.id || "").trim();
      if (!id || choices.get(id)?.virtual) return;
      const existing = choices.get(id) || { id, upstreams: new Set() };
      existing.upstreams.add(String(item.upstream || "默认上游"));
      choices.set(id, existing);
    });
    const previous = sessionStorage.getItem(modelKey) || select.value;
    select.replaceChildren();
    const sorted = [...choices.values()].sort((left, right) => {
      if (Boolean(left.virtual) !== Boolean(right.virtual)) return left.virtual ? -1 : 1;
      return left.id.localeCompare(right.id, "zh-CN");
    });
    if (!sorted.length) {
      const option = new Option("没有可用模型", "");
      select.append(option);
      select.disabled = true;
      if (modelResult.status === "rejected") notify(modelResult.reason.message, true);
      return;
    }
    sorted.forEach((item) => {
      const upstreams = item.upstreams ? [...item.upstreams] : [];
      const label = item.label || `${item.id} · ${upstreams.length > 1 ? `${upstreams.length} 个上游自动路由` : upstreams[0] || "自动路由"}`;
      select.append(new Option(label, item.id));
    });
    select.disabled = false;
    select.value = choices.has(previous) ? previous : sorted[0].id;
    sessionStorage.setItem(modelKey, select.value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function parseChatContent(payload) {
    const message = payload?.choices?.[0]?.message;
    if (typeof message?.content === "string") return message.content;
    if (Array.isArray(message?.content)) {
      return message.content.map((part) => typeof part === "string" ? part : part?.text || "").join("");
    }
    return "";
  }

  async function consumeStream(response, onText) {
    const reader = response.body?.getReader();
    if (!reader) throw new Error("浏览器无法读取流式响应");
    const decoder = new TextDecoder();
    let pending = "";
    let completed = false;
    const handleFrame = (frame) => {
      const raw = frame.split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n")
        .trim();
      if (!raw) return;
      if (raw === "[DONE]") {
        completed = true;
        return;
      }
      let payload;
      try {
        payload = JSON.parse(raw);
      } catch (_) {
        return;
      }
      if (payload?.error) throw new Error(errorMessage(payload, "上游流式响应失败"));
      const delta = payload?.choices?.[0]?.delta?.content;
      if (typeof delta === "string" && delta) onText(delta);
    };
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = pending.split(/\r?\n\r?\n/);
      pending = frames.pop() || "";
      frames.forEach(handleFrame);
      if (done) break;
    }
    if (pending.trim()) handleFrame(pending);
    if (!completed) throw new Error("流式响应未收到标准结束标志 [DONE]");
  }

  async function showTrace(requestId, startedAt) {
    if (!requestId) {
      $("#chat-request-meta").textContent = `总耗时 ${Math.round(performance.now() - startedAt)} ms`;
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
    try {
      const result = await adminRequest(`/api/traces?request_id=${encodeURIComponent(requestId)}&limit=1`);
      const trace = result.data?.[0];
      if (!trace) throw new Error("trace pending");
      const parts = [
        `请求 ID ${requestId}`,
        trace.upstream ? `上游 ${trace.upstream}` : "",
        trace.first_byte_ms != null ? `首字 ${trace.first_byte_ms} ms` : "",
        trace.latency_ms != null ? `总耗时 ${trace.latency_ms} ms` : "",
        Number(trace.attempts) > 1 ? `尝试 ${trace.attempts} 次` : "",
      ].filter(Boolean);
      $("#chat-request-meta").textContent = parts.join(" · ");
    } catch (_) {
      $("#chat-request-meta").textContent = `请求 ID ${requestId} · 总耗时 ${Math.round(performance.now() - startedAt)} ms`;
    }
  }

  async function sendMessage(event) {
    event.preventDefault();
    if (state.controller) return;
    const input = $("#chat-input");
    const content = input.value.trim();
    const model = $("#chat-model").value;
    if (!model) return notify("请先选择模型", true);
    if (!content) return notify("请输入消息", true);

    state.messages.push({ role: "user", content });
    const outbound = state.messages.slice(-29);
    const assistantIndex = state.messages.length;
    state.messages.push({ role: "assistant", content: "" });
    input.value = "";
    renderMessages();
    setBusy(true);
    $("#chat-request-meta").textContent = "正在建立上游连接…";
    const controller = new AbortController();
    state.controller = controller;
    const startedAt = performance.now();
    let answer = "";
    let requestId = "";
    try {
      const messages = [];
      const systemPrompt = $("#chat-system-prompt").value.trim();
      if (systemPrompt) messages.push({ role: "system", content: systemPrompt });
      messages.push(...outbound);
      const streaming = $("#chat-stream").checked;
      const response = await fetch("/api/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, messages, stream: streaming }),
        signal: controller.signal,
      });
      if (response.status === 401) {
        location.replace("/login");
        return;
      }
      requestId = response.headers.get("X-Request-ID") || "";
      state.requestId = requestId;
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(errorMessage(data));
      }
      if (streaming) {
        await consumeStream(response, (delta) => {
          answer += delta;
          setAssistantText(assistantIndex, answer);
        });
      } else {
        const data = await response.json();
        if (data?.error) throw new Error(errorMessage(data));
        answer = parseChatContent(data);
        setAssistantText(assistantIndex, answer);
      }
      if (!answer) {
        answer = "模型未返回可显示的文本内容。";
        setAssistantText(assistantIndex, answer);
      }
      saveSession();
      renderMessages();
      await showTrace(requestId, startedAt);
    } catch (error) {
      if (error.name === "AbortError") {
        $("#chat-state").textContent = "已停止";
        $("#chat-request-meta").textContent = requestId ? `请求 ID ${requestId} · 已由用户停止` : "已由用户停止";
      } else {
        notify(error.message || "对话请求失败", true);
        $("#chat-state").textContent = "请求失败";
        $("#chat-request-meta").textContent = requestId ? `请求 ID ${requestId} · ${error.message}` : error.message;
      }
      if (!answer) state.messages.splice(assistantIndex, 1);
      else saveSession();
      renderMessages();
    } finally {
      state.controller = null;
      setBusy(false);
      input.focus();
    }
  }

  function closeNoteModal() {
    $("#admin-note-modal")?.remove();
  }

  window.openAdminNote = async () => {
    closeNoteModal();
    const overlay = document.createElement("div");
    overlay.id = "admin-note-modal";
    overlay.className = "cleanllm-modal";
    overlay.innerHTML = '<form class="cleanllm-modal-card admin-note-card"><div><p class="eyebrow">管理员工具</p><h3>备注</h3><p class="admin-note-help">用于临时记录实例相关事项，保存后同一实例的其他设备也可查看。</p></div><label class="field"><span>记事内容</span><textarea id="admin-note-text" rows="12" maxlength="10000" placeholder="例如：待测试的模型、临时维护安排或上游说明"></textarea><small id="admin-note-count">0 / 10000</small></label><div class="cleanllm-modal-actions"><button id="admin-note-cancel" class="button" type="button">取消</button><button id="admin-note-save" class="button primary" type="submit">保存备注</button></div></form>';
    document.body.append(overlay);
    const textarea = $("#admin-note-text");
    const count = $("#admin-note-count");
    const syncCount = () => { count.textContent = `${textarea.value.length} / 10000`; };
    textarea.disabled = true;
    textarea.placeholder = "正在读取备注…";
    textarea.addEventListener("input", syncCount);
    $("#admin-note-cancel").onclick = closeNoteModal;
    overlay.addEventListener("click", (event) => { if (event.target === overlay) closeNoteModal(); });
    try {
      const data = await adminRequest("/api/account/note");
      textarea.disabled = false;
      textarea.placeholder = "例如：待测试的模型、临时维护安排或上游说明";
      textarea.value = data.note || "";
      syncCount();
      textarea.focus();
    } catch (error) {
      closeNoteModal();
      notify(error.message, true);
      return;
    }
    overlay.querySelector("form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#admin-note-save");
      button.disabled = true;
      try {
        const result = await adminRequest("/api/account/note", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note: textarea.value }),
        });
        closeNoteModal();
        notify(result.message);
      } catch (error) {
        notify(error.message, true);
        button.disabled = false;
      }
    });
  };

  $("#chat-form").addEventListener("submit", sendMessage);
  $("#chat-stop").addEventListener("click", () => state.controller?.abort());
  $("#chat-clear").addEventListener("click", async () => {
    const approved = window.cleanllmConfirm
      ? await window.cleanllmConfirm("清空当前浏览器标签页中的对话？")
      : true;
    if (!approved) return;
    state.messages = [];
    sessionStorage.removeItem(sessionKey);
    $("#chat-request-meta").textContent = "尚未发送请求";
    renderMessages();
  });
  $("#chat-model").addEventListener("change", (event) => {
    if (event.target.value) sessionStorage.setItem(modelKey, event.target.value);
  });
  $("#chat-system-prompt").addEventListener("change", saveSession);
  $("#chat-stream").addEventListener("change", saveSession);
  $("#chat-input").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") $("#chat-form").requestSubmit();
  });
  $("#chat-messages").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-message]");
    if (!button) return;
    try {
      await copyText(state.messages[Number(button.dataset.copyMessage)]?.content || "");
      notify("回答已复制");
    } catch (_) {
      notify("复制失败，请手动选择内容", true);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("#admin-note-modal")) closeNoteModal();
  });

  loadSession();
  renderMessages();
  loadModels().catch((error) => notify(error.message, true));
})();
