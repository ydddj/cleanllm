(() => {
  const $ = (selector) => document.querySelector(selector);
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const request = async (url, options) => {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "请求失败");
    return data;
  };
  const notify = (message, error = false) => typeof toast === "function" ? toast(message, error) : alert(message);
  const nav = document.querySelector(".nav");
  const securityPage = document.querySelector('[data-view="security"] .panel-body');
  if (securityPage && !document.querySelector("#restart-container")) {
    securityPage.insertAdjacentHTML("beforeend", '<div style="margin-top:18px;padding-top:16px;border-top:1px solid var(--border)"><button id="restart-container" class="button" style="color:var(--red)">重启容器</button><p class="form-note">仅重启 CleanLLM 容器，不会删除设置或模型。</p></div>');
    $("#restart-container").onclick = async () => { if (!confirm("确定重启 CleanLLM 容器？")) return; try { const result=await request("/api/system/restart", {method:"POST"}); notify(result.message); } catch(error) { notify(error.message, true); } };
  }
  if (nav && !document.querySelector('[data-page="changelog"]')) {
    nav.insertAdjacentHTML("beforeend", '<a href="#changelog" data-page="changelog"><span>▤</span><span>更新日志</span></a>');
    document.querySelector(".content-wrap")?.insertAdjacentHTML("beforeend", '<section class="page" data-view="changelog"><section class="panel"><div id="changelog-content" class="panel-body">正在读取更新日志…</div></section></section>');
    request("/api/changelog").then((data) => { $("#changelog-content").innerHTML = `<pre style="white-space:pre-wrap;font:13px/1.7 inherit">${escape(data.content)}</pre>`; }).catch((error) => { $("#changelog-content").textContent = error.message; });
    nav.querySelector('[data-page="changelog"]').addEventListener("click", (event) => {
      event.preventDefault();
      document.querySelectorAll(".nav a").forEach((link) => link.classList.toggle("active", link.dataset.page === "changelog"));
      document.querySelectorAll(".page").forEach((view) => view.classList.toggle("active", view.dataset.view === "changelog"));
      $("#page-eyebrow").textContent = "系统"; $("#page-title").textContent = "更新日志"; $("#page-description").textContent = "查看 CleanLLM 的功能更新与修复记录"; $("#page-actions").innerHTML = "";
    });
  }

  const patterns = $("#clean_patterns")?.closest(".field");
  if (patterns) {
    patterns.insertAdjacentHTML("beforebegin", `<label class="field wide"><span>备用上游（JSON）</span><textarea id="upstreams" rows="6" spellcheck="false" placeholder='[{"name":"备用","url":"http://server/v1/chat/completions","api_key":""}]'></textarea><small>默认上游失败后按顺序切换。</small></label><label class="field wide"><span>模型路由（JSON）</span><textarea id="model_routes" rows="5" spellcheck="false" placeholder='[{"pattern":"qwen*","upstreams":["默认上游","备用"]}]'></textarea><small>支持 * 和 ? 通配符。</small></label>`);
    const footer = $("#settings-form .form-footer");
    footer.insertAdjacentHTML("beforebegin", `<div class="wide" style="display:flex;gap:9px;flex-wrap:wrap"><button id="save-routing" class="button primary" type="button">保存路由设置</button><button id="test-connections" class="button" type="button">测试全部连接</button><button id="export-settings" class="button" type="button">导出脱敏配置</button><label class="button" style="display:inline-flex;align-items:center">导入配置<input id="import-settings" type="file" accept="application/json" hidden></label><button id="reset-settings" class="button" type="button" style="color:var(--red)">恢复默认值</button></div><div id="connection-results" class="wide"></div>`);
    request("/api/settings").then((data) => {
      $("#upstreams").value = JSON.stringify(data.upstreams || [], null, 2);
      $("#model_routes").value = JSON.stringify(data.model_routes || [], null, 2);
    }).catch(() => {});
  }

  $("#save-routing")?.addEventListener("click", async () => {
    try {
      const current = await request("/api/settings");
      current.upstreams = JSON.parse($("#upstreams").value || "[]");
      current.model_routes = JSON.parse($("#model_routes").value || "[]");
      await request("/api/settings", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(current)});
      notify("多上游与模型路由已保存");
    } catch (error) { notify(`保存失败：${error.message}`, true); }
  });
  $("#test-connections")?.addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      const data = await request("/api/settings/test", {method:"POST"});
      $("#connection-results").innerHTML = data.results.map((item) => `<div style="padding:8px 0;color:${item.ok?'var(--green)':'var(--red)'}"><strong>${item.ok?'✓':'×'} ${escape(item.name)}</strong> · ${escape(item.detail)} ${item.latency_ms ? `(${item.latency_ms} ms)` : ""}</div>`).join("");
    } catch (error) { notify(error.message, true); } finally { event.currentTarget.disabled = false; }
  });
  $("#export-settings")?.addEventListener("click", () => location.assign("/api/settings/export"));
  $("#import-settings")?.addEventListener("change", async (event) => {
    try {
      const data = JSON.parse(await event.target.files[0].text());
      await request("/api/settings/import", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({settings:data.settings || data})});
      notify("配置已导入，正在刷新"); setTimeout(() => location.reload(), 600);
    } catch (error) { notify(`导入失败：${error.message}`, true); }
  });
  $("#reset-settings")?.addEventListener("click", async () => {
    if (!confirm("恢复代理默认设置？账户信息不会改变。")) return;
    try { await request("/api/settings/reset", {method:"POST"}); location.reload(); } catch (error) { notify(error.message, true); }
  });

  const copyButtons = () => {
    document.querySelectorAll("#models-content tbody td:first-child, #ollama-models tbody td:first-child").forEach((cell) => {
      if (cell.querySelector(".copy-model")) return;
      const name = cell.querySelector("code")?.textContent;
      if (!name) return;
      const button = document.createElement("button");
      button.className = "copy-model"; button.title = "复制名称"; button.setAttribute("aria-label", "复制名称"); button.textContent = "⧉";
      button.style.cssText = "margin-left:8px;border:0;background:transparent;color:var(--primary);font-size:17px";
      button.onclick = async () => { try { await navigator.clipboard.writeText(name); } catch (_) { const area=document.createElement("textarea"); area.value=name; document.body.append(area); area.select(); document.execCommand("copy"); area.remove(); } notify(`已复制：${name}`); };
      cell.append(button);
    });
    document.querySelectorAll("#ollama-models tbody tr").forEach((row) => {
      const action = row.lastElementChild, model = row.querySelector("code")?.textContent;
      if (!action || !model || action.querySelector(".advanced-model-actions")) return;
      const controls = document.createElement("span"); controls.className = "advanced-model-actions";
      controls.innerHTML = `<button class="button" data-model-action="detail">详情</button> <button class="button" data-model-action="duplicate">克隆模型</button> <button class="button" data-model-action="load">加载</button> <button class="button" data-model-action="unload">卸载</button> <button class="button" data-model-action="export">导出</button>`;
      action.prepend(controls);
      controls.querySelectorAll("button").forEach((button) => button.onclick = (event) => { event.stopPropagation(); modelAction(model, button.dataset.modelAction); });
    });
  };
  new MutationObserver(copyButtons).observe(document.body, {childList:true, subtree:true});
  copyButtons();

  async function modelAction(model, action) {
    try {
      if (action === "detail") {
        const detail = await request(`/api/ollama/models/${encodeURIComponent(model)}`), info = detail.details || {};
        showModal(`<h3>模型详情</h3><p><strong>${escape(model)}</strong></p><dl><dt>系列</dt><dd>${escape(info.family || '—')}</dd><dt>参数</dt><dd>${escape(info.parameter_size || '—')}</dd><dt>量化</dt><dd>${escape(info.quantization_level || '—')}</dd><dt>格式</dt><dd>${escape(info.format || '—')}</dd><dt>上下文</dt><dd>${escape(detail.model_info?.['llama.context_length'] || detail.model_info?.['qwen2.context_length'] || '—')}</dd></dl>`);
      } else if (action === "duplicate") {
        const destination = await askModal("克隆模型", "克隆后的模型名称", `${model}-copy`); if (!destination) return;
        const result = await request("/api/ollama/copy", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({source:model,destination})}); notify(result.message); if(typeof loadOllama === "function") loadOllama();
      } else if (action === "export") { location.assign(`/api/ollama/models/${encodeURIComponent(model)}/export`);
      } else {
        const result = await request("/api/ollama/keep-alive", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model,keep_alive:action==='unload'?'0':'5m'})}); notify(result.message);
      }
    } catch (error) { notify(error.message, true); }
  }

  function showModal(content) { let modal=$("#cleanllm-modal"); if(!modal){document.body.insertAdjacentHTML("beforeend",'<div id="cleanllm-modal" style="position:fixed;inset:0;background:#0008;z-index:200;display:grid;place-items:center"><div style="background:var(--surface);color:var(--text);padding:24px;border-radius:16px;width:min(520px,92vw);max-height:80vh;overflow:auto"><div id="cleanllm-modal-body"></div><button class="button primary" id="close-cleanllm-modal">关闭</button></div></div>');modal=$("#cleanllm-modal");$("#close-cleanllm-modal").onclick=()=>modal.remove();} $("#cleanllm-modal-body").innerHTML=content; }
  function askModal(title,label,value){ return new Promise((resolve)=>{showModal(`<h3>${title}</h3><label class="field"><span>${label}</span><input id="modal-input" value="${escape(value)}"></label><button class="button primary" id="modal-ok">确定</button>`);$("#modal-ok").onclick=()=>{const result=$("#modal-input").value;$("#cleanllm-modal").remove();resolve(result);};}); }

  const ollamaPanel = $("#ollama-panel .panel-body");
  if (ollamaPanel) ollamaPanel.insertAdjacentHTML("beforeend", `<div style="margin-top:18px;border-top:1px solid var(--border);padding-top:16px"><div style="display:flex;justify-content:space-between;align-items:center"><strong>后台拉取任务</strong><button id="background-pull" class="button">后台拉取当前模型</button></div><div id="ollama-tasks" style="margin-top:10px"></div></div>`);
  if (ollamaPanel) ollamaPanel.insertAdjacentHTML("beforeend", '<div style="margin-top:12px"><label class="button">导入模型定义<input id="import-model" type="file" accept=".json,application/json" hidden></label></div>');
  $("#import-model")?.addEventListener("change", async (event) => { try { const data=JSON.parse(await event.target.files[0].text()); const result=await request("/api/ollama/models/import", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:data.name,modelfile:data.modelfile || data.template || ""})}); notify(result.message); if(typeof loadOllama === "function") loadOllama(); } catch(error) { notify(`导入失败：${error.message}`,true); } });
  $("#background-pull")?.addEventListener("click", async () => {
    const model = $("#ollama-model-name").value.trim();
    if (!model) return notify("请输入模型名称", true);
    try { await request("/api/ollama/tasks", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model})}); notify("后台任务已创建"); refreshTasks(); } catch (error) { notify(error.message, true); }
  });
  async function refreshTasks() {
    if (!$("#ollama-tasks")) return;
    try {
      const data = await request("/api/ollama/tasks");
      $("#ollama-tasks").innerHTML = data.data.length ? data.data.map((task) => { const percent=task.total?Math.round(task.completed/task.total*100):0; return `<div style="display:grid;grid-template-columns:1fr 2fr auto;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)"><code>${escape(task.model)}</code><span>${escape(task.message)} · ${percent}%</span><span>${task.status==='running'?`<button class="button" data-cancel="${task.id}">取消</button>`:(['failed','cancelled'].includes(task.status)?`<button class="button" data-retry="${task.id}">重试</button>`:escape(task.status))}</span></div>`; }).join("") : '<small class="form-note">暂无后台任务</small>';
      document.querySelectorAll("[data-cancel]").forEach((button) => button.onclick=()=>taskAction(button.dataset.cancel,"cancel"));
      document.querySelectorAll("[data-retry]").forEach((button) => button.onclick=()=>taskAction(button.dataset.retry,"retry"));
    } catch (_) {}
  }
  async function taskAction(id, action) { try { await request(`/api/ollama/tasks/${id}/${action}`, {method:"POST"}); refreshTasks(); } catch(error) { notify(error.message,true); } }
  refreshTasks(); setInterval(refreshTasks, 2000);

  document.addEventListener("click", async (event) => {
    const row = event.target.closest("#ollama-models tbody tr");
    if (!row || event.target.closest("button")) return;
    const model = row.querySelector("code")?.textContent;
    if (!model) return;
    try {
      const detail = await request(`/api/ollama/models/${encodeURIComponent(model)}`);
      const info = detail.details || {};
      alert(`模型：${model}\n系列：${info.family || '—'}\n参数：${info.parameter_size || '—'}\n量化：${info.quantization_level || '—'}\n格式：${info.format || '—'}\n\n点击确定后可使用行内删除操作；加载/卸载 API 已开放。`);
    } catch (error) { notify(error.message, true); }
  });
})();
