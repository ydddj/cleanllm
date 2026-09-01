(() => {
  const polish = document.createElement("style"); polish.textContent = ".content-wrap{width:100%;max-width:1800px}.advanced-model-actions{display:inline-flex;flex-wrap:wrap;gap:6px;margin-right:8px}.advanced-model-actions .button{min-height:34px;padding:0 11px}.data-table td{vertical-align:middle}.data-table td:first-child code{overflow-wrap:anywhere}.log-view,.log-line{font-size:11px!important}.cleanllm-modal{position:fixed;inset:0;z-index:200;display:grid;place-items:center;padding:20px;background:rgba(0,0,0,.62);backdrop-filter:blur(4px)}.cleanllm-modal-card{width:min(520px,100%);max-height:80vh;overflow:auto;padding:26px;border:1px solid var(--border);border-radius:18px;background:var(--surface);color:var(--text);box-shadow:var(--shadow)}.cleanllm-modal-card h3{margin:0 0 20px;font-size:20px}.cleanllm-modal-actions{display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-top:24px}.cleanllm-modal-actions .button{min-width:82px}.cleanllm-modal-card .field input{margin-top:2px}#cleanllm-modal dl{display:grid;grid-template-columns:90px 1fr;gap:10px;margin:18px 0}#cleanllm-modal dt{color:var(--muted)}#cleanllm-modal dd{margin:0;overflow-wrap:anywhere}@media(max-width:760px){.content-wrap{padding:20px 12px 40px}.data-table{min-width:760px}.advanced-model-actions{margin-bottom:6px}.cleanllm-modal-card{padding:20px}.cleanllm-modal-actions{flex-direction:row;justify-content:stretch}.cleanllm-modal-actions .button{flex:1}}"; document.head.append(polish);
  const $ = (selector) => document.querySelector(selector);
  if (typeof showPage === "function") showPage(); if (typeof loadModels === "function") loadModels(); document.documentElement.classList.add("js-ready"); document.body.style.visibility="visible";
  const sprite=document.querySelector('.icon-sprite'); if(sprite&&!document.getElementById('i-palette')) sprite.insertAdjacentHTML('beforeend','<symbol id="i-palette" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="9" cy="9" r="1"/><circle cx="15" cy="8" r="1"/><circle cx="16" cy="14" r="1"/><path d="M8 16h2a2 2 0 0 1 2 2v2"/></symbol><symbol id="i-chevron" viewBox="0 0 24 24"><path d="m7 9 5 5 5-5"/></symbol><symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2"/></symbol>');
  if (typeof pages === "object") pages["api-tokens"] = ["系统", "API 访问令牌", "管理访问令牌、用量和使用日志"];
  const savedTheme = localStorage.getItem("cleanllm-theme") || "dark";
  document.documentElement.dataset.theme = savedTheme === "light" ? "light" : "dark";
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const request = async (url, options) => {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "请求失败");
    return data;
  };
  const notify = (message, error = false) => typeof toast === "function" ? toast(message, error) : console.log(message);
  const palettes = [
    ["靛蓝", "#4f46e5", "#3730a3"], ["海蓝", "#2563eb", "#1d4ed8"], ["青色", "#0891b2", "#0e7490"], ["翡翠", "#059669", "#047857"], ["青柠", "#65a30d", "#4d7c0f"], ["琥珀", "#d97706", "#b45309"], ["珊瑚", "#ea580c", "#c2410c"], ["玫红", "#e11d48", "#be123c"], ["紫罗兰", "#7c3aed", "#6d28d9"], ["洋红", "#c026d3", "#a21caf"]
  ];
  const paletteStyle = document.createElement("style"); paletteStyle.textContent = ".palette-menu{position:fixed;z-index:120;display:grid;grid-template-columns:repeat(2,1fr);gap:7px;padding:12px;border:1px solid var(--border);border-radius:14px;background:var(--surface);box-shadow:var(--shadow)}.palette-item{display:flex;align-items:center;gap:7px;padding:7px 9px;border:1px solid transparent;border-radius:8px;background:var(--soft);color:var(--text);font-size:11px}.palette-item:hover{border-color:var(--primary)}.palette-dot{width:16px;height:16px;border-radius:50%}.brand-mark{background:linear-gradient(145deg,var(--primary),var(--primary2))!important}.button.primary{background:linear-gradient(180deg,var(--primary),var(--primary2))!important}.palette-item[data-selected=true]{border-color:var(--primary)}.service-pill,.status-badge.active{color:var(--primary)!important;background:var(--primary-soft)!important}.data-table button[style*='var(--red)'],#reset-settings{color:var(--primary)!important}"; document.head.append(paletteStyle);
  const layout = document.createElement("style"); layout.textContent = ".content-wrap{max-width:none!important;width:100%;margin:0}html:not(.js-ready) .page[data-view=dashboard]{display:block}.panel{width:100%}.log-line .level{color:var(--primary)!important}.button{cursor:pointer!important}.stats-grid{grid-template-columns:repeat(3,minmax(0,1fr))!important}@media(max-width:980px){.stats-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important}}@media(max-width:600px){.stats-grid{grid-template-columns:1fr!important}}"; document.head.append(layout);
  const nav = document.querySelector(".nav");
  const securityLink = nav?.querySelector('[data-page="security"]'), logsLink = nav?.querySelector('[data-page="logs"]'); if (securityLink && logsLink) nav.insertBefore(securityLink, logsLink);
  const versionLabel = document.querySelector(".sidebar-status small"); if (versionLabel) versionLabel.textContent = "CleanLLM v1.0.21";
  const modelPage = document.querySelector('[data-view="models"]'), ollama = document.querySelector("#ollama-panel"); if (modelPage && ollama) modelPage.appendChild(ollama);
  const topActions = document.querySelector(".topbar-actions");
  if ($("#theme-button")) $("#theme-button").title = "切换深浅主题";
  if (topActions && !document.querySelector("#palette-toggle")) { topActions.insertAdjacentHTML("afterbegin", '<button id="palette-toggle" class="icon-button" title="切换配色" aria-label="切换配色">◉</button>'); $("#palette-toggle").onclick=()=>{let menu=$("#palette-menu");if(menu){menu.remove();return}document.body.insertAdjacentHTML("beforeend",'<div id="palette-menu" class="palette-menu">'+palettes.map((item,index)=>`<button class="palette-item" data-palette="${index}"><i class="palette-dot" style="background:${item[1]}"></i>${item[0]}</button>`).join("")+"</div>");document.querySelectorAll("[data-palette]").forEach((button)=>button.onclick=()=>{const item=palettes[button.dataset.palette];document.documentElement.style.setProperty("--primary",item[1]);document.documentElement.style.setProperty("--primary2",item[2]);document.documentElement.style.setProperty("--primary-soft",item[1]+"26");localStorage.setItem("cleanllm-palette",button.dataset.palette);$("#palette-menu").remove()})};const saved=Number(localStorage.getItem("cleanllm-palette"));if(Number.isInteger(saved)&&palettes[saved]){const item=palettes[saved];document.documentElement.style.setProperty("--primary",item[1]);document.documentElement.style.setProperty("--primary2",item[2]);document.documentElement.style.setProperty("--primary-soft",item[1]+"26")}}
  if (topActions && document.querySelector("#palette-toggle")) { const paletteButton=$("#palette-toggle"); paletteButton.onclick=(event)=>{event.stopPropagation();let menu=$("#palette-menu");if(menu){menu.remove();return}document.body.insertAdjacentHTML("beforeend",'<div id="palette-menu" class="palette-menu">'+palettes.map((item,index)=>`<button class="palette-item" data-palette="${index}"><i class="palette-dot" style="background:${item[1]}"></i>${item[0]}</button>`).join("")+"</div>");menu=$("#palette-menu");const rect=paletteButton.getBoundingClientRect();menu.style.top=`${rect.bottom+8}px`;menu.style.right=`${Math.max(12,window.innerWidth-rect.right)}px`;menu.querySelectorAll("[data-palette]").forEach((button)=>button.onclick=()=>{const item=palettes[button.dataset.palette];document.documentElement.style.setProperty("--primary",item[1]);document.documentElement.style.setProperty("--primary2",item[2]);document.documentElement.style.setProperty("--primary-soft",item[1]+"26");localStorage.setItem("cleanllm-palette",button.dataset.palette);menu.remove()})};document.addEventListener("click",(event)=>{const menu=$("#palette-menu");if(menu&&!event.target.closest("#palette-menu")&&!event.target.closest("#palette-toggle"))menu.remove()}) }
  if ($("#restart-container")) $("#restart-container").onclick = async () => { if (!(await window.cleanllmConfirm?.("确认重启 CleanLLM 容器？"))) return; try { const result=await request("/api/system/restart",{method:"POST"}); notify(result.message); } catch(e){ notify(e.message,true); } };
  if ($("#logout") && !$("#user-menu")) { $("#logout").onclick=(event)=>{event.stopPropagation();let menu=$("#user-menu");if(menu){menu.remove();return}document.body.insertAdjacentHTML("beforeend",'<div id="user-menu" class="user-menu"><button id="menu-logout" class="button" style="color:var(--red)">退出登录</button></div>');menu=$("#user-menu");const rect=$("#logout").getBoundingClientRect();menu.style.position="fixed";menu.style.top=`${rect.bottom+8}px`;menu.style.right="18px";menu.style.padding="10px";menu.style.background="var(--surface)";menu.style.border="1px solid var(--border)";menu.style.borderRadius="12px";menu.querySelector("#menu-logout").onclick=async()=>{await fetch("/api/logout",{method:"POST"});location.assign("/login")};};document.addEventListener("click",(event)=>{if(!event.target.closest("#user-menu")&&!event.target.closest("#logout"))$("#user-menu")?.remove()}) }
  const savedPalette = Number(localStorage.getItem("cleanllm-palette")); if (Number.isInteger(savedPalette) && palettes[savedPalette]) { const item=palettes[savedPalette]; document.documentElement.style.setProperty("--primary",item[1]); document.documentElement.style.setProperty("--primary2",item[2]); document.documentElement.style.setProperty("--primary-soft",item[1]+"26"); }
  if (topActions && !document.querySelector("#restart-container")) {
    topActions.insertAdjacentHTML("afterbegin", '<button id="restart-container" class="icon-button" title="重启容器" aria-label="重启容器">↻</button>');
    $("#restart-container").onclick = () => new Promise((resolve) => { const modal=createModal('<h3>重启容器</h3><p>重启只会重新启动 CleanLLM，不会删除设置或模型。</p>','<button class="button" id="restart-cancel">取消</button><button class="button primary" id="restart-ok">确认重启</button>'); $("#restart-cancel").onclick=()=>{modal.remove();resolve()}; $("#restart-ok").onclick=async()=>{try{const result=await request("/api/system/restart",{method:"POST"});modal.remove();notify(result.message);resolve()}catch(error){notify(error.message,true)}}; });
  }
  if (nav && !document.querySelector('[data-page="changelog"]')) {
    nav.insertAdjacentHTML("beforeend", '<a href="#changelog" data-page="changelog"><span>▤</span><span>更新日志</span></a>');
    document.querySelector(".content-wrap")?.insertAdjacentHTML("beforeend", '<section class="page" data-view="changelog"><section class="panel"><div id="changelog-content" class="panel-body">正在读取更新日志…</div></section></section>');
    request("/api/changelog").then((data) => { const sections=data.content.split(/(?=^## )/m).filter(Boolean), content=$("#changelog-content"); let shown=2; const render=()=>{content.innerHTML=sections.slice(0,shown).map((section)=>`<pre style="white-space:pre-wrap;font:11px/1.6 inherit;margin:0 0 18px">${escape(section)}</pre>`).join("")+`<div style="display:flex;gap:9px"><button id="changelog-more" class="button" ${shown>=sections.length?'hidden':''}>加载更多</button><button id="changelog-less" class="button" ${shown<=2?'hidden':''}>收起历史</button></div>`; $("#changelog-more")?.addEventListener("click",()=>{shown=Math.min(shown+2,sections.length);render()});$("#changelog-less")?.addEventListener("click",()=>{shown=2;render()});}; render(); }).catch((error) => { $("#changelog-content").textContent = error.message; });
    const activateChangelog = () => {
      document.querySelectorAll(".nav a").forEach((link) => link.classList.toggle("active", link.dataset.page === "changelog"));
      document.querySelectorAll(".page").forEach((view) => view.classList.toggle("active", view.dataset.view === "changelog"));
      $("#page-eyebrow").textContent = "系统"; $("#page-title").textContent = "更新日志"; $("#page-description").textContent = "查看 CleanLLM 的功能更新与修复记录"; $("#page-actions").innerHTML = "";
    }; nav.querySelector('[data-page="changelog"]').addEventListener("click", () => { location.hash="changelog"; setTimeout(activateChangelog, 0); });
    if (location.hash === "#changelog") setTimeout(activateChangelog, 0);
  }

  const themeButton = $("#theme-button");
  if (themeButton) { const syncThemeIcon=()=>themeButton.querySelector("use")?.setAttribute("href",document.documentElement.dataset.theme === "dark" ? "#i-sun" : "#i-moon"); syncThemeIcon(); themeButton.onclick = () => { const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; localStorage.setItem("cleanllm-theme", next); syncThemeIcon(); }; }
  const patterns = $("#clean_patterns")?.closest(".field");
  const logNote = document.querySelector('[data-view="dashboard"] .stat-card.orange small');
  request("/api/settings").then((data) => { if (logNote) logNote.textContent = `当前日志上限 ${Math.round((data.log_max_bytes || 5242880) / 1048576)} MB`; }).catch(() => {});
  const logsDescription = document.querySelector('[data-view="logs"] .panel-header p'); if (logsDescription) logsDescription.textContent = "仅记录请求状态与系统事件，大小上限可在此调整";
  const logPage = document.querySelector('[data-view="logs"] .panel-header');
  if (logPage) logPage.insertAdjacentHTML("beforeend", '<label style="display:flex;align-items:center;gap:7px;font-size:12px">日志上限 <input id="log-max-mb" type="number" min="1" max="50" step="1" style="width:68px;height:32px;padding:0 7px;border:1px solid var(--border);border-radius:8px;background:var(--soft)"> MB <button id="save-log-limit" class="button">保存</button></label>');
  if (logPage) logPage.insertAdjacentHTML("beforeend", '<label style="display:flex;align-items:center;gap:7px;font-size:12px">日志级别 <select id="log-level" style="height:32px;border:1px solid var(--border);border-radius:8px;background:var(--soft)"><option value="WARNING">WARN</option><option value="INFO">INFO</option><option value="ERROR">ERROR</option><option value="DEBUG">DEBUG</option></select></label>');
  if (logPage) logPage.insertAdjacentHTML("beforeend", '<button id="clear-logs" class="button" style="color:var(--red)">清除日志</button>');
  $("#clear-logs")?.addEventListener("click", async()=>{if(!await window.cleanllmConfirm?.("确认清除全部系统日志？"))return;try{const result=await request("/api/logs",{method:"DELETE"});notify(result.message);if(typeof loadLogs==="function")loadLogs()}catch(e){notify(e.message,true)}});
  request("/api/settings").then((data) => { if ($("#log-max-mb")) $("#log-max-mb").value = Math.round((data.log_max_bytes || 5242880) / 1048576); if ($("#log-level")) $("#log-level").value = data.log_level || "WARNING"; }).catch(() => {});
  $("#save-log-limit")?.addEventListener("click", async () => { try { const current=await request("/api/settings"), mb=Number($("#log-max-mb").value); current.log_max_bytes=mb*1048576; current.log_level=$("#log-level")?.value||"WARNING"; await request("/api/settings", {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(current)}); notify(`日志设置已保存：${current.log_level}`); } catch(error) { notify(error.message,true); } });
  if (patterns) {
    patterns.insertAdjacentHTML("beforebegin", `<label class="field wide"><span>备用上游（JSON）</span><textarea id="upstreams" rows="6" spellcheck="false" placeholder='[{"name":"备用","url":"http://server/v1/chat/completions","api_key":""}]'></textarea><small>默认上游失败后按顺序切换。</small></label><label class="field wide"><span>模型路由（JSON）</span><textarea id="model_routes" rows="5" spellcheck="false" placeholder='[{"pattern":"qwen*","upstreams":["默认上游","备用"]}]'></textarea><small>支持 * 和 ? 通配符。</small></label>`);
    const footer = $("#settings-form .form-footer");
    footer.insertAdjacentHTML("beforebegin", `<div class="wide" style="display:flex;gap:9px;flex-wrap:wrap"><button id="test-connections" class="button" type="button">测试全部连接</button><button id="export-settings" class="button" type="button">导出脱敏配置</button><label class="button" style="display:inline-flex;align-items:center">导入配置<input id="import-settings" type="file" accept="application/json" hidden></label><button id="reset-settings" class="button" type="button" style="color:var(--red)">恢复默认值</button></div><div id="connection-results" class="wide"></div>`);
    request("/api/settings").then((data) => {
      $("#upstreams").value = data.upstreams?.length ? JSON.stringify(data.upstreams, null, 2) : "";
      $("#model_routes").value = data.model_routes?.length ? JSON.stringify(data.model_routes, null, 2) : "";
    }).catch(() => {});
  }

  $("#settings-form")?.addEventListener("submit", async (event) => {
    event.preventDefault(); event.stopImmediatePropagation();
    const button=event.currentTarget.querySelector('button[type="submit"]'); button.disabled=true;
    try {
      const current=await request("/api/settings");
      Object.assign(current,{target_api_url:$("#target_api_url").value,api_key:$("#api_key").value,timeout_seconds:Number($("#timeout_seconds").value),models_api_url:$("#models_api_url").value,ollama_api_url:$("#ollama_api_url").value,upstreams:JSON.parse($("#upstreams").value||"[]"),model_routes:JSON.parse($("#model_routes").value||"[]"),clean_patterns:$("#clean_patterns").value.split("\n").map(line=>line.trim()).filter(Boolean)});
      await request("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(current)}); notify("全部设置已保存");
    } catch(error) { notify(`保存失败：${error.message}`,true); } finally { button.disabled=false; }
  }, true);
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
    if (!(await (window.cleanllmConfirm ? window.cleanllmConfirm("恢复代理默认设置？账户信息不会改变。") : Promise.resolve(true)))) return;
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
      controls.innerHTML = `<button class="button" data-model-action="detail">详情</button> <button class="button" data-model-action="duplicate">克隆模型</button> <button class="button" data-model-action="load">加载</button> <button class="button" data-model-action="unload">卸载</button> <button class="button" data-model-action="archive">导出压缩包</button> <button class="button" data-model-action="export">导出定义</button>`;
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
      } else if (action === "archive") { location.assign(`/api/ollama/archive?model=${encodeURIComponent(model)}`);
      } else if (action === "export") { location.assign(`/api/ollama/export?model=${encodeURIComponent(model)}`);
      } else {
        const result = await request("/api/ollama/keep-alive", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model,keep_alive:action==='unload'?'0':'5m'})}); notify(result.message);
      }
    } catch (error) { notify(error.message, true); }
  }

  function createModal(content, actions) { $("#cleanllm-modal")?.remove(); document.body.insertAdjacentHTML("beforeend",`<div id="cleanllm-modal" class="cleanllm-modal"><div class="cleanllm-modal-card"><div id="cleanllm-modal-body">${content}</div><div class="cleanllm-modal-actions">${actions}</div></div></div>`); return $("#cleanllm-modal"); }
  function showModal(content) { const modal=createModal(content,'<button class="button primary" id="close-cleanllm-modal">关闭</button>'); $("#close-cleanllm-modal").onclick=()=>modal.remove(); }
  function askModal(title,label,value){ return new Promise((resolve)=>{const modal=createModal(`<h3>${title}</h3><label class="field"><span>${label}</span><input id="modal-input" value="${escape(value)}"></label>`,'<button class="button" id="modal-cancel">取消</button><button class="button primary" id="modal-ok">确定</button>');$("#modal-cancel").onclick=()=>{modal.remove();resolve(null)};$("#modal-ok").onclick=()=>{const result=$("#modal-input").value.trim();modal.remove();resolve(result)};$("#modal-input").focus();}); }

  const ollamaPanel = $("#ollama-panel .panel-body");
  if (ollamaPanel) ollamaPanel.insertAdjacentHTML("beforeend", `<div style="margin-top:18px;border-top:1px solid var(--border);padding-top:16px"><div style="display:flex;justify-content:space-between;align-items:center"><strong>后台拉取任务</strong><button id="background-pull" class="button">后台拉取当前模型</button></div><div id="ollama-tasks" style="margin-top:10px"></div></div>`);
  if (ollamaPanel) ollamaPanel.insertAdjacentHTML("beforeend", '<div style="margin-top:18px;border-top:1px solid var(--border);padding-top:16px"><div style="display:flex;justify-content:space-between;align-items:center"><strong>导出任务历史</strong><button id="refresh-export-history" class="button">刷新</button></div><div id="export-history" style="margin-top:10px"></div></div>');
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
  async function refreshExportHistory(){const box=$("#export-history");if(!box)return;try{const data=await request("/api/ollama/export-history");box.innerHTML=data.data.length?data.data.map(item=>`<div style="display:grid;grid-template-columns:1fr auto auto;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)"><code>${escape(item.model)}</code><span>${escape(item.size?`${(item.size/1048576).toFixed(2)} MB`:'—')}</span><small>${new Date(Number(item.created_at)*1000).toLocaleString()}</small></div>`).join(""):"<small class=\"form-note\">暂无导出记录</small>"}catch(e){box.textContent=e.message}}
  $("#refresh-export-history")?.addEventListener("click",refreshExportHistory); refreshExportHistory();

  window.cleanllmConfirm = (message) => new Promise((resolve) => {
    const modal = createModal(`<h3>请确认</h3><p>${escape(message)}</p>`, '<button class="button" id="modal-confirm-no">取消</button><button class="button primary" id="modal-confirm-yes">确认</button>');
    $("#modal-confirm-no").onclick = () => { modal.remove(); resolve(false); };
    $("#modal-confirm-yes").onclick = () => { modal.remove(); resolve(true); };
  });

  // Lightweight real-time status stream and token/export management panels.
  if (nav && !nav.querySelector('[data-page="api-tokens"]')) { const modelLink=nav.querySelector('[data-page="models"]'); modelLink?.insertAdjacentHTML("afterend", '<a href="#api-tokens" data-page="api-tokens"><span>⌁</span><span>API 访问令牌</span></a>'); }
  if (!document.querySelector('[data-view="api-tokens"]')) document.querySelector(".content-wrap")?.insertAdjacentHTML("beforeend", '<section class="page" data-view="api-tokens"></section>');
  const tokenPage = document.querySelector('[data-view="api-tokens"]');
  nav?.querySelector('[data-page="api-tokens"]')?.addEventListener("click", (event) => { event.preventDefault(); location.hash="api-tokens"; document.querySelectorAll(".nav a,.page").forEach((el)=>el.classList.toggle("active", el.dataset.page==="api-tokens" || el.dataset.view==="api-tokens")); $("#page-eyebrow").textContent="系统"; $("#page-title").textContent="API 访问令牌"; $("#page-description").textContent="管理访问令牌、用量和使用日志"; $("#page-actions").innerHTML=""; });
  if (tokenPage && !document.querySelector("#token-panel")) tokenPage.innerHTML = '<section id="token-panel" class="panel"><header class="panel-header"><div><h2>API 访问令牌</h2><p>管理客户端调用令牌与使用情况</p></div><button id="new-api-token" class="button primary">创建令牌</button></header><div class="panel-body"><div id="token-list" class="info-list"></div></div></section><section class="panel" style="margin-top:18px"><header class="panel-header"><div><h2>用量概览</h2><p>最近 30 天的令牌调用统计</p></div><span id="usage-total" class="tag">0 次</span></header><div id="token-usage" class="panel-body"></div></section>';
  if (!document.querySelector("#dashboard-usage")) document.querySelector('[data-view="dashboard"] .stats-grid')?.insertAdjacentHTML("beforeend", '<article id="dashboard-usage" class="stat-card"><span>API 用量</span><strong id="dashboard-usage-count">—</strong><small>最近 24 小时令牌调用</small></article>');
  async function loadTokens(){const box=$("#token-list");if(!box)return;try{const data=await request("/api/tokens");box.innerHTML=data.data.length?data.data.map(t=>`<div><span>${escape(t.name)}<small class="form-note">创建于 ${new Date(Number(t.created_at)*1000).toLocaleString()}</small><code class="token-value">${escape(t.token||"不可恢复")}</code></span><span style="display:flex;gap:7px"><button class="icon-button copy-token" data-token="${escape(t.token||"")}" title="复制令牌" aria-label="复制令牌">⧉</button><button class="button" data-revoke-token="${escape(t.id)}" style="color:var(--red)">撤销</button></span></div>`).join(""):"<p class=\"form-note\">尚未创建令牌</p>";box.querySelectorAll(".copy-token").forEach(b=>b.onclick=()=>copyToken(b.dataset.token));box.querySelectorAll("[data-revoke-token]").forEach(b=>b.onclick=async()=>{if(await window.cleanllmConfirm("撤销此令牌？")){await request(`/api/tokens/${b.dataset.revokeToken}`,{method:"DELETE"});loadTokens();}})}catch(e){notify(e.message,true)}}
  const copyToken=async(token)=>{if(!token)return notify("令牌不可用",true);try{await navigator.clipboard.writeText(token);notify("令牌已复制")}catch(_){const area=document.createElement("textarea");area.value=token;area.style.position="fixed";area.style.opacity="0";document.body.append(area);area.select();try{document.execCommand("copy");notify("令牌已复制")}catch(__){notify("复制失败，请手动选择令牌",true)}area.remove()}};
  const tokenMaskObserver=new MutationObserver(()=>{$("#token-list")?.querySelectorAll(".token-value").forEach((el)=>{if(el.dataset.masked)return;const raw=el.textContent||"",masked=raw.length>11?`${raw.slice(0,7)}${"*".repeat(raw.length-11)}${raw.slice(-4)}`:raw;el.dataset.raw=raw;el.dataset.masked="1";el.textContent=masked;const eye=document.createElement("button");eye.className="icon-button token-eye";eye.textContent="◉";eye.title="显示/隐藏令牌";eye.setAttribute("aria-label","显示/隐藏令牌");eye.onclick=()=>{const visible=el.dataset.visible==="1";el.textContent=visible?masked:el.dataset.raw;el.dataset.visible=visible?"0":"1"};const actions=el.closest("div")?.querySelector(".copy-token")?.parentElement;if(actions)actions.insertBefore(eye,actions.firstChild);});});$("#token-list")&&tokenMaskObserver.observe($("#token-list"),{childList:true,subtree:true});
  $("#new-api-token")?.addEventListener("click",async()=>{const name=await askModal("创建 API 令牌","令牌名称","我的客户端");if(!name)return;try{const t=await request("/api/tokens",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});const modal=createModal(`<h3>令牌创建成功</h3><p>令牌已安全保存，之后可在列表中再次复制：</p><div style="display:flex;gap:8px;align-items:center"><code class="endpoint-code" style="flex:1">${escape(t.token)}</code><button class="icon-button" id="copy-new-token" title="复制令牌" aria-label="复制令牌">⧉</button></div>`,'<button class="button primary" id="close-token-modal">关闭</button>');$("#copy-new-token").onclick=()=>copyToken(t.token);$("#close-token-modal").onclick=()=>modal.remove();loadTokens()}catch(e){notify(e.message,true)}});
  loadTokens();
  async function loadUsage(){const box=$("#token-usage");if(!box)return;try{const data=await request("/api/usage");$("#usage-total").textContent=`${data.total} 次`;box.innerHTML=`<div class="stats-grid" style="grid-template-columns:repeat(2,1fr)"><article class="stat-card"><span>今日调用</span><strong>${data.today}</strong></article><article class="stat-card"><span>活跃令牌</span><strong>${Object.keys(data.by_token||{}).length}</strong></article></div><h3 style="font-size:14px">使用日志</h3>${(data.logs||[]).length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>令牌</th><th>接口</th></tr></thead><tbody>${data.logs.map(i=>`<tr><td>${escape(new Date(Number(i.at)*1000).toLocaleString())}</td><td>${escape(i.token_name)}</td><td><code>${escape(i.method)} ${escape(i.path)}</code></td></tr>`).join("")}</tbody></table></div>`:'<p class="form-note">暂无使用记录</p>'}`}catch(e){box.textContent=e.message}}
  loadUsage(); setInterval(loadUsage, 10000); setInterval(async()=>{try{const usage=await request("/api/usage");if($("#dashboard-usage-count")) $("#dashboard-usage-count").textContent=usage.today;}catch(_){ }},10000);
  const eventSource = new EventSource("/api/system/events"); const markOnline=()=>document.querySelectorAll(".service-pill,.status-badge.active").forEach((el)=>{el.style.color="var(--primary)";}); eventSource.addEventListener("connected",markOnline); eventSource.addEventListener("request",markOnline); eventSource.onerror = () => document.querySelectorAll(".service-pill,.status-badge.active").forEach((el)=>{el.style.opacity=".6";}); eventSource.onopen=()=>document.querySelectorAll(".service-pill,.status-badge.active").forEach((el)=>{el.style.opacity="1";});
  const modelsHeader = document.querySelector('[data-view="models"] .panel-header');
  if (modelsHeader && !document.querySelector("#model-cache-settings")) { modelsHeader.insertAdjacentHTML("beforeend", '<label id="model-cache-settings" style="display:flex;align-items:center;gap:7px;font-size:12px">缓存 <input id="model-cache-ttl" type="number" min="0" max="86400" style="width:68px;height:32px;padding:0 7px;border:1px solid var(--border);border-radius:8px;background:var(--soft)"> 秒 <button id="save-model-cache" class="button">保存</button></label>'); request("/api/settings").then(s=>{ $("#model-cache-ttl").value=s.model_cache_ttl ?? 60; }); $("#save-model-cache").onclick=async()=>{try{const s=await request("/api/settings"),ttl=Number($("#model-cache-ttl").value);s.model_cache_ttl=ttl;await request("/api/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(s)});notify("模型缓存策略已保存")}catch(e){notify(e.message,true)}}; }
  const originalLoadModels = window.loadModels;
  if (typeof originalLoadModels === "function") window.loadModels = () => originalLoadModels();
  document.addEventListener("click", async (event) => { if (!event.target.closest("#refresh-models")) return; event.preventDefault(); event.stopImmediatePropagation(); const button=event.target.closest("#refresh-models"); button.disabled=true; try { await request("/api/models?refresh=true"); if(typeof loadModels==="function") await loadModels(); } catch(e){ notify(e.message,true); } finally { button.disabled=false; } }, true);

  document.addEventListener("click", async (event) => {
    const row = event.target.closest("#ollama-models tbody tr");
    if (!row || event.target.closest("button")) return;
    const model = row.querySelector("code")?.textContent;
    if (!model) return;
    try {
      const detail = await request(`/api/ollama/models/${encodeURIComponent(model)}`);
      const info = detail.details || {};
      showModal(`<h3>模型详情</h3><p><strong>${escape(model)}</strong></p><dl><dt>系列</dt><dd>${escape(info.family || '—')}</dd><dt>参数</dt><dd>${escape(info.parameter_size || '—')}</dd><dt>量化</dt><dd>${escape(info.quantization_level || '—')}</dd><dt>格式</dt><dd>${escape(info.format || '—')}</dd></dl>`);
    } catch (error) { notify(error.message, true); }
  });
})();
