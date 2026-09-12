const pages={dashboard:["工作台","概览","查看代理、模型和日志的运行状态"],upstream:["代理管理","上游与清洗","配置上游接口、模型发现和响应清洗规则"],models:["代理管理","模型列表","按上游查看可用模型并管理 Ollama 模型"],chat:["工作台","对话测试","通过真实路由验证模型、流式输出与清洗效果"],logs:["系统","系统日志","查看最近运行记录，日志大小可在页面调整"],changelog:["系统","更新日志","查看 CleanLLM 的功能更新与修复记录"],security:["系统","账户安全","管理 Web 登录用户名和密码"],"api-tokens":["系统","API令牌","管理访问令牌、用量和使用日志"],diagnostics:["系统","诊断中心","检查路由、上游兼容性和请求链路"],analytics:["工作台","分析与成本","查看性能、Token、错误率和估算费用"]};let settings={},account={},models=[],modelsLoading=false,modelUpstreams=[],activeModelUpstream="",ollamaModels=[],logs=[],logShown=100,logMeta={size_bytes:0,max_bytes:5242880};const LOG_PAGE_SIZE=100,$=selector=>document.querySelector(selector),$$=selector=>[...document.querySelectorAll(selector)];
const patternsField=$('#clean_patterns').closest('.field');patternsField.insertAdjacentHTML('beforebegin','<label class="field wide"><span>Ollama 管理地址（可选）</span><input id="ollama_api_url" type="url" placeholder="例如 http://host.docker.internal:11434"><small>留空时从上游 API 地址自动提取。</small></label>');const modelPage=$('[data-view="models"]');modelPage.insertAdjacentHTML('afterbegin','<section id="ollama-panel" class="panel ollama-panel"><header class="panel-header"><div><h2>Ollama 模型管理</h2><p id="ollama-status">正在检测服务…</p></div><span id="ollama-version" class="tag">检测中</span></header><div class="panel-body"><div class="ollama-pull-workspace"><div class="ollama-pull-row"><input id="ollama-model-name" aria-label="要拉取的 Ollama 模型名称" placeholder="输入模型名称，例如 qwen3:8b"><button id="pull-ollama-model" class="button primary" type="button">拉取模型</button><button id="background-pull" class="button" type="button">后台拉取</button></div><div id="ollama-progress" class="ollama-foreground-progress" hidden><div><span id="ollama-progress-status">准备中…</span><strong id="ollama-progress-percent">0%</strong></div><progress id="ollama-progress-bar" max="100" value="0"></progress></div><section class="ollama-task-section" aria-labelledby="ollama-tasks-title"><div class="ollama-section-heading"><div><strong id="ollama-tasks-title">后台拉取任务</strong><small>任务在后台继续运行，可在此查看进度</small></div><span id="ollama-task-count" class="tag">0 个任务</span></div><div id="ollama-tasks" class="ollama-task-list" aria-live="polite"><small class="form-note">暂无后台任务</small></div></section></div><div id="ollama-models" class="ollama-model-list"></div></div></section>');
function escapeHtml(value){return String(value??"").replace(/[&<>'"]/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]))}async function dataOf(response){const type=response.headers.get("content-type")||"";return type.includes("application/json")?response.json():{detail:await response.text()}}function detailOf(data,fallback){return Array.isArray(data.detail)?data.detail.map(item=>item.msg).join("；"):data.detail||fallback}function unauthorized(response){if(response.status===401){location.assign("/login");return true}return false}function toast(text,error=false){const node=document.createElement("div");node.className=`toast${error?" error":""}`;node.textContent=text==="Failed to fetch"?"连接服务失败，请稍后重试":text;$("#toasts").append(node);setTimeout(()=>node.remove(),3500)}function bytes(value){if(value<1024)return `${value} B`;if(value<1048576)return `${(value/1024).toFixed(1)} KB`;return `${(value/1048576).toFixed(2)} MB`}
async function api(url,options){const response=await fetch(url,options);if(unauthorized(response))throw new Error("请先登录");const data=await dataOf(response);if(!response.ok)throw new Error(detailOf(data,"请求失败"));return data}
function currentPage(){const name=location.hash.slice(1);return pages[name]?name:"dashboard"}function showPage(){const page=currentPage(),meta=pages[page];$$('.nav a').forEach(link=>link.classList.toggle('active',link.dataset.page===page));$$('.page').forEach(view=>view.classList.toggle('active',view.dataset.view===page));$('#page-eyebrow').textContent=meta[0];$('#page-title').textContent=meta[1];$('#page-description').textContent=meta[2];$('#global-search').value='';$('#page-actions').innerHTML=page==='models'?'<button id="refresh-models" class="button"><svg><use href="#i-refresh"/></svg>刷新模型</button>':page==='logs'?'<button id="refresh-logs" class="button"><svg><use href="#i-refresh"/></svg>刷新日志</button>':'';if(page==='models'){$('#refresh-models').onclick=loadModels;if(!models.length)loadModels()}if(page==='logs'){$('#refresh-logs').onclick=loadLogs;loadLogs()}closeMenu()}
async function loadSettings(){settings=await api('/api/settings');['target_api_url','api_key','timeout_seconds','models_api_url','ollama_api_url'].forEach(name=>{$(`#${name}`).value=settings[name]??''});$('#clean_patterns').value=(settings.clean_patterns||[]).join('\n');$('#dash-upstream').textContent=settings.target_api_url;$('#dash-upstream').title=settings.target_api_url;$('#dash-model-url').textContent=settings.models_api_url||'自动推导 /v1/models';$('#client-endpoint').textContent=`${location.origin}/v1/chat/completions`}
async function loadAccount(){account=await api('/api/account');$('#account_username').value=account.username;$('#top-username').textContent=account.username;$('#dash-user').textContent=account.username;$('#user-avatar').textContent=account.username.slice(0,1).toUpperCase()}
async function loadModels(){if(modelsLoading)return;modelsLoading=true;const button=$('#refresh-models');if(button)button.disabled=true;$('#models-content').innerHTML='<div class="empty-state"><div><h3>正在获取模型…</h3><p>连接上游模型发现接口。</p></div></div>';try{const result=await api('/api/models');models=result.data||[];modelUpstreams=result.upstreams||[...new Set(models.map(model=>model.upstream||"默认上游"))];if(!modelUpstreams.includes(activeModelUpstream))activeModelUpstream=modelUpstreams[0]||"";$('#models-source').textContent=result.source;$('#nav-model-count').textContent=result.count;$('#dash-models').textContent=result.count;renderModels()}catch(error){models=[];$('#models-content').innerHTML=`<div class="empty-state"><div><h3>模型获取失败</h3><p>${escapeHtml(error.message)}</p></div></div>`;toast(error.message,true)}finally{modelsLoading=false;if(button)button.disabled=false}}
function formatModelTime(value){if(value===null||value===undefined||value==='')return '—';let raw=value;if(typeof raw==='number'||/^\d+$/.test(String(raw))){raw=Number(raw);if(raw<1e12)raw*=1000}const date=new Date(raw);return Number.isNaN(date.getTime())?String(value):date.toLocaleString('zh-CN',{hour12:false})}
function formatContextLength(value){const size=Number(value||0);if(!size)return '未知';if(size>=1000000)return `${(size/1000000).toFixed(size%1000000?1:0)}M`;if(size>=1000)return `${(size/1000).toFixed(size%1000?1:0)}K`;return String(size)}
function renderModels(){const query=$('#global-search').value.trim().toLowerCase(),sourceModels=models.filter(model=>(model.upstream||"默认上游")===activeModelUpstream),filtered=sourceModels.filter(model=>`${model.id} ${model.owned_by} ${model.upstream||""} ${(model.capabilities||[]).join(' ')} ${(model.interfaces||[]).join(' ')}`.toLowerCase().includes(query));$('#models-count').textContent=`${sourceModels.length} 个模型`;const tabs=`<div class="upstream-model-tabs">${modelUpstreams.map(name=>`<button type="button" class="button${name===activeModelUpstream?' active':''}" data-model-upstream="${escapeHtml(name)}">${escapeHtml(name)}</button>`).join('')}</div>`;const capabilityLabels={vision:'视觉',tools:'工具',tool_calling:'工具',reasoning:'推理',embedding:'向量',audio:'音频'};const labels=value=>(value||[]).map(item=>`<span class="model-capability">${escapeHtml(capabilityLabels[item]||item)}</span>`).join('')||'<span class="muted-value">未声明</span>';const thinkingSelect=model=>{const upstream=model.upstream||"默认上游",mode=window.cleanllmModelThinkingMode?.(upstream,model.id)||"";return `<select class="model-thinking-select" data-thinking-upstream="${escapeHtml(upstream)}" data-thinking-model="${escapeHtml(model.id)}" aria-label="${escapeHtml(model.id)} 的思考模式"><option value="" ${mode===''?'selected':''}>承上游</option><option value="disabled" ${mode==='disabled'?'selected':''}>关闭</option><option value="enabled" ${mode==='enabled'?'selected':''}>开启</option><option value="low" ${mode==='low'?'selected':''}>低强度</option><option value="medium" ${mode==='medium'?'selected':''}>中强度</option><option value="high" ${mode==='high'?'selected':''}>高强度</option></select>`};const table=filtered.length?`<div class="table-wrap"><table class="data-table model-table"><thead><tr><th>模型 ID</th><th>来源上游</th><th>提供方</th><th>思考模式</th><th>上下文</th><th>能力</th><th>可用接口</th><th>创建 / 更新时间</th></tr></thead><tbody>${filtered.map(model=>`<tr><td><code>${escapeHtml(model.id)}</code></td><td><span class="tag">${escapeHtml(model.upstream||"默认上游")}</span></td><td><span class="tag">${escapeHtml(model.owned_by)}</span></td><td>${thinkingSelect(model)}</td><td>${escapeHtml(formatContextLength(model.context_length))}</td><td><div class="model-tags">${labels(model.capabilities)}</div></td><td><div class="model-tags">${labels(model.interfaces)}</div></td><td>${escapeHtml(formatModelTime(model.created))}</td></tr>`).join('')}</tbody></table></div>`:`<div class="empty-state"><svg><use href="#i-box"/></svg><div><h3>${query?'没有匹配的模型':'该上游暂无可用模型'}</h3><p>${query?'请尝试其他关键词。':'请检查模型列表地址或上游鉴权。'}</p></div></div>`;$('#models-content').innerHTML=tabs+table;$$('[data-model-upstream]').forEach(button=>button.onclick=()=>{activeModelUpstream=button.dataset.modelUpstream;renderModels()})}
async function loadOllama(){try{const status=await api('/api/ollama/status');if(!status.available){$('#ollama-status').textContent='未检测到 Ollama；仍可查看 OpenAI 兼容模型';$('#ollama-version').textContent='不可用';$('#pull-ollama-model').disabled=true;$('#background-pull').disabled=true;$('#ollama-models').innerHTML='';return}$('#ollama-status').textContent=status.base_url;$('#ollama-version').textContent=`Ollama ${status.version}`;$('#pull-ollama-model').disabled=false;$('#background-pull').disabled=false;const result=await api('/api/ollama/models');ollamaModels=result.data||[];$('#ollama-models').innerHTML=ollamaModels.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>已安装模型</th><th>大小</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${ollamaModels.map(model=>`<tr><td><code>${escapeHtml(model.id)}</code></td><td>${model.size?bytes(model.size):'—'}</td><td>${escapeHtml(formatModelTime(model.modified_at))}</td><td><button class="button danger compact" data-delete-model="${escapeHtml(model.id)}">删除</button></td></tr>`).join('')}</tbody></table></div>`:'<p class="form-note">Ollama 中尚无已安装模型。</p>';$$('[data-delete-model]').forEach(button=>button.onclick=()=>deleteOllamaModel(button.dataset.deleteModel))}catch(error){$('#ollama-status').textContent=error.message;$('#ollama-version').textContent='不可用';$('#pull-ollama-model').disabled=true;$('#background-pull').disabled=true}}
async function deleteOllamaModel(model){const approved=window.cleanllmConfirm?await window.cleanllmConfirm(`确定删除模型 ${model}？此操作不可撤销。`):true;if(!approved)return;try{const result=await api('/api/ollama/models',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});toast(result.message);await Promise.all([loadOllama(),loadModels()])}catch(error){toast(error.message,true)}}
async function pullOllamaModel(){const model=$('#ollama-model-name').value.trim(),button=$('#pull-ollama-model');if(!model){toast('请输入要拉取的模型名称',true);return}button.disabled=true;$('#ollama-progress').hidden=false;try{const response=await fetch('/api/ollama/pull',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});if(unauthorized(response))return;if(!response.ok)throw new Error(detailOf(await dataOf(response),'拉取失败'));const reader=response.body.getReader(),decoder=new TextDecoder();let pending='';while(true){const {value,done}=await reader.read();pending+=decoder.decode(value||new Uint8Array(),{stream:!done});const lines=pending.split('\n');pending=lines.pop()||'';for(const line of lines){if(!line.trim())continue;const item=JSON.parse(line);if(item.error)throw new Error(item.error);$('#ollama-progress-status').textContent=item.status||'处理中';const percent=item.total?Math.round((item.completed||0)/item.total*100):0;$('#ollama-progress-bar').value=percent;$('#ollama-progress-percent').textContent=`${percent}%`}if(done)break}toast(`模型 ${model} 拉取完成`);await Promise.all([loadOllama(),loadModels()])}catch(error){$('#ollama-progress-status').textContent=`失败：${error.message}`;toast(error.message,true)}finally{button.disabled=false}}
async function loadLogs(){try{const result=await api('/api/logs?limit=800');logs=result.lines||[];logMeta=result;$('#log-size').textContent=`${bytes(result.size_bytes)} / ${bytes(result.max_bytes)}`;$('#dash-log-size').textContent=bytes(result.size_bytes);$('#log-meter-fill').style.width=`${Math.min(100,result.size_bytes/result.max_bytes*100)}%`;renderLogs()}catch(error){toast(error.message,true)}}function renderLogs(){const view=$('#log-view'),scrollTop=view.scrollTop,query=$('#global-search').value.trim().toLowerCase(),filtered=logs.filter(line=>line.toLowerCase().includes(query)).reverse();if(!filtered.length){view.innerHTML='<div class="empty-state"><div><h3>暂无匹配日志</h3><p>新请求会显示在这里。</p></div></div>';return}const shown=Math.min(logShown,filtered.length);view.innerHTML=filtered.slice(0,shown).map(line=>{const parts=line.split(' | '),time=parts.shift()||'',level=parts.shift()||'INFO',message=parts.join(' | ');return `<div class="log-line"><span>${escapeHtml(time)}</span><span class="level ${escapeHtml(level)}">${escapeHtml(level)}</span><span>${escapeHtml(message)}</span></div>`}).join('')+`<div class="log-history-actions"><button id="log-more" class="button" ${shown>=filtered.length?'hidden':''}>加载更多</button><button id="log-less" class="button" ${logShown<=LOG_PAGE_SIZE?'hidden':''}>收起历史</button><small>已显示 ${shown} / ${filtered.length} 条</small></div>`;view.scrollTop=scrollTop;$('#log-more')?.addEventListener('click',()=>{logShown=Math.min(logShown+LOG_PAGE_SIZE,filtered.length);renderLogs()});$('#log-less')?.addEventListener('click',()=>{logShown=LOG_PAGE_SIZE;renderLogs();view.scrollTop=0})}
$('#settings-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button[type=submit]');button.disabled=true;try{settings=await api('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_api_url:$('#target_api_url').value,api_key:$('#api_key').value,timeout_seconds:Number($('#timeout_seconds').value),models_api_url:$('#models_api_url').value,ollama_api_url:$('#ollama_api_url').value,upstreams:settings.upstreams||[],model_routes:settings.model_routes||[],clean_patterns:$('#clean_patterns').value.split('\n').map(line=>line.trim()).filter(Boolean)})});toast(settings.message||'代理设置已保存');await loadSettings()}catch(error){toast(error.message,true)}finally{button.disabled=false}});
$('#account-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.currentTarget.querySelector('button[type=submit]'),password=$('#new_password').value;if(password!==$('#confirm_password').value){toast('两次输入的新密码不一致',true);return}button.disabled=true;try{const result=await api('/api/account',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('#account_username').value,current_password:$('#current_password').value,new_password:password})});toast(result.message);setTimeout(()=>location.assign('/login'),900)}catch(error){toast(error.message,true)}finally{button.disabled=false}});
$('#pull-ollama-model').onclick=pullOllamaModel;$('.reveal').onclick=event=>{const input=$('#api_key');input.type=input.type==='password'?'text':'password';event.currentTarget.textContent=input.type==='password'?'显示':'隐藏'};$('#logout').onclick=async()=>{await fetch('/api/logout',{method:'POST'});location.assign('/login')};$('#global-search').addEventListener('input',()=>{if(currentPage()==='models')renderModels();if(currentPage()==='logs')renderLogs()});document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();$('#global-search').focus()}});function closeMenu(){$('#sidebar').classList.remove('open');$('#backdrop').classList.remove('open')}$('#menu-button').onclick=()=>{$('#sidebar').classList.add('open');$('#backdrop').classList.add('open')};$('#backdrop').onclick=closeMenu;$('#theme-button').onclick=()=>{const dark=document.documentElement.dataset.theme!=='dark';document.documentElement.dataset.theme=dark?'dark':'';localStorage.setItem('cleanllm-theme',dark?'dark':'light')};if(localStorage.getItem('cleanllm-theme')==='dark')document.documentElement.dataset.theme='dark';window.addEventListener('hashchange',showPage);setInterval(()=>{if(!document.hidden&&currentPage()==='logs')loadLogs()},5000);const cleanllmInitialLoad=Promise.all([loadSettings(),loadAccount(),loadOllama()]).then(showPage).catch(error=>toast(error.message,true));
// Render the URL-selected shell immediately; slow data requests hydrate it in the background.
let logsRequest = null;
const loadLogsOnce = loadLogs;
loadLogs = (...args) => {
  if (logsRequest) return logsRequest;
  logsRequest = Promise.resolve(loadLogsOnce(...args)).finally(() => { logsRequest = null; });
  return logsRequest;
};
loadLogs();
showPage();

// Ollama updates are checked separately from the installed-model list so the
// normal model manager stays responsive when the public registry is slow or
// does not know about a private/local model.
const cleanllmBaseLoadOllama = loadOllama;
let cleanllmUpdateCheck = null;
let cleanllmOllamaUpdates = [];
function ensureOllamaUpdateButton() {
  const pullRow = document.querySelector(".ollama-pull-row");
  if (!pullRow) return;
  if (!$("#check-ollama-updates")) {
    pullRow.insertAdjacentHTML("beforeend", '<button id="check-ollama-updates" class="button" type="button" disabled>检查更新</button>');
  }
  const button = $("#check-ollama-updates");
  if (button.dataset.bound !== "true") {
    button.addEventListener("click", checkOllamaUpdates);
    button.dataset.bound = "true";
  }
}
function syncOllamaUpdateButton() {
  const button = $("#check-ollama-updates");
  if (!button || cleanllmUpdateCheck) return;
  const unavailable = $("#ollama-version")?.textContent === "不可用";
  button.disabled = unavailable;
  button.title = unavailable ? "Ollama 不可用，无法检查模型更新" : "";
}
function renderOllamaUpdateCells(updates) {
  const table = $("#ollama-models .data-table");
  if (!table) return;
  const header = table.querySelector("thead tr");
  if (header && !header.querySelector("[data-ollama-update-heading]")) {
    const heading = document.createElement("th");
    heading.dataset.ollamaUpdateHeading = "";
    heading.textContent = "更新状态";
    header.lastElementChild?.before(heading);
  }
  const map = new Map((updates || []).map(item => [String(item.id), item]));
  table.querySelectorAll("tbody tr").forEach(row => {
    const code = row.querySelector("td code");
    if (!code) return;
    const model = code.textContent.trim();
    const item = map.get(model);
    let content = '<span class="muted-value">未检查</span>';
    if (updates && updates.length && (!item || item.state === "unknown")) {
      const detail = item?.detail || "私有仓库、本地模型或公共仓库暂不可用";
      content = `<span class="muted-value" title="${escapeHtml(detail)}">无法检查</span>`;
    }
    if (item?.state === "latest") content = '<span class="status-badge active">最新</span>';
    if (item?.state === "update") content = `<button class="button primary compact" type="button" data-ollama-update="${escapeHtml(model)}">更新模型</button>`;
    if (item?.state === "updating") content = '<span class="status-badge active">更新中</span>';
    let cell = row.querySelector("[data-ollama-update-cell]");
    if (!cell) {
      cell = document.createElement("td");
      cell.dataset.ollamaUpdateCell = "";
      row.lastElementChild?.before(cell);
    }
    cell.innerHTML = content;
  });
  $$('[data-ollama-update]').forEach(button => {
    button.addEventListener("click", async () => {
      const model = button.dataset.ollamaUpdate;
      button.disabled = true;
      try {
        await api("/api/ollama/tasks", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({model})});
        cleanllmOllamaUpdates = cleanllmOllamaUpdates.map(item => item.id === model ? {...item, state: "updating", update_available: false} : item);
        renderOllamaUpdateCells(cleanllmOllamaUpdates);
        window.cleanllmRefreshOllamaTasks?.();
        toast(`模型 ${model} 的更新任务已开始`);
      } catch (error) {
        button.disabled = false;
        toast(error.message, true);
      }
    }, {once: true});
  });
}
async function checkOllamaUpdates() {
  if (cleanllmUpdateCheck) return cleanllmUpdateCheck;
  const button = $("#check-ollama-updates");
  if (button) { button.disabled = true; button.textContent = "检查中…"; }
  cleanllmUpdateCheck = api("/api/ollama/models/updates").then(result => {
    cleanllmOllamaUpdates = result.data || [];
    renderOllamaUpdateCells(cleanllmOllamaUpdates);
    const found = cleanllmOllamaUpdates.filter(item => item.update_available).length;
    const unknown = cleanllmOllamaUpdates.some(item => item.state === "unknown");
    if (button) button.textContent = found ? `发现 ${found} 个更新` : unknown ? "检查完成" : "已是最新";
    return result;
  }).catch(error => {
    if (button) button.textContent = "检查更新";
    if (error.message !== "请先登录") toast(error.message, true);
    return null;
  }).finally(() => {
    cleanllmUpdateCheck = null;
    syncOllamaUpdateButton();
  });
  return cleanllmUpdateCheck;
}
loadOllama = async function loadOllamaWithUpdates() {
  await cleanllmBaseLoadOllama();
  ensureOllamaUpdateButton();
  renderOllamaUpdateCells(cleanllmOllamaUpdates);
  syncOllamaUpdateButton();
};
ensureOllamaUpdateButton();
cleanllmInitialLoad.then(() => {
  renderOllamaUpdateCells(cleanllmOllamaUpdates);
  syncOllamaUpdateButton();
});

// Match notify-router's single-query behavior: the top field searches the
// current page, including data that is rendered or refreshed after typing.
const cleanllmSearchItemSelector = [
  "tbody tr", ".diagnostic-item", ".release-note", ".snapshot-list > article",
  ".pricing-row", ".virtual-model-row", ".health-row", ".ollama-task-item",
  ".stats-grid > .stat-card", ".analytics-stats > article", ".feature-list > div",
  ".info-list > div", ".log-line", ".appearance-thumb"
].join(",");
function cleanllmSearchCandidates(page) {
  return [...page.querySelectorAll(":scope > .panel, :scope > .stats-grid > .stat-card, :scope > .dashboard-grid > .panel, :scope > .settings-grid > .panel")];
}
function cleanllmSearchableText(node) {
  if (!node) return "";
  const controls = [...node.querySelectorAll("input, textarea, select")].map(control => {
    const option = control.selectedOptions?.[0]?.textContent || "";
    return `${control.value || ""} ${control.placeholder || ""} ${option}`;
  }).join(" ");
  return `${node.textContent || ""} ${node.getAttribute?.("title") || ""} ${node.getAttribute?.("aria-label") || ""} ${controls}`.toLocaleLowerCase("zh-CN");
}
function cleanllmSetSearchVisibility(node, visible) {
  node.hidden = !visible;
  if (visible) delete node.dataset.searchHidden;
  else node.dataset.searchHidden = "1";
}
function cleanllmPanelSearch(panel, query) {
  const items = [...panel.querySelectorAll(cleanllmSearchItemSelector)].filter(item =>
    !item.parentElement?.closest(cleanllmSearchItemSelector)
  );
  if (!query) {
    items.forEach(item => cleanllmSetSearchVisibility(item, true));
    cleanllmSetSearchVisibility(panel, true);
    return true;
  }
  if (!items.length) {
    const matches = cleanllmSearchableText(panel).includes(query);
    cleanllmSetSearchVisibility(panel, matches);
    return matches;
  }
  const headerMatches = cleanllmSearchableText(panel.querySelector(":scope > .panel-header")).includes(query);
  let matches = 0;
  items.forEach(item => {
    const visible = headerMatches || cleanllmSearchableText(item).includes(query);
    cleanllmSetSearchVisibility(item, visible);
    if (visible) matches += 1;
  });
  cleanllmSetSearchVisibility(panel, headerMatches || matches > 0);
  return headerMatches || matches > 0;
}
function cleanllmApplyPageSearch() {
  const page = document.querySelector(`.page[data-view="${currentPage()}"]`);
  const input = $("#global-search");
  if (!page || !input) return;
  const query = input.value.trim().toLocaleLowerCase("zh-CN");
  if (currentPage() === "logs") return;
  const candidates = cleanllmSearchCandidates(page);
  let empty = page.querySelector(".global-search-empty");
  if (!empty) { page.insertAdjacentHTML("beforeend", '<div class="empty-state global-search-empty" hidden><div><h3>没有匹配内容</h3><p>请尝试其他关键词。</p></div></div>'); empty = page.querySelector(".global-search-empty"); }
  const visible = candidates.reduce((count, node) => count + (cleanllmPanelSearch(node, query) ? 1 : 0), 0);
  empty.hidden = !query || visible > 0;
}
function cleanllmResetPageSearch() {
  document.querySelectorAll("[data-search-hidden]").forEach(node => { node.hidden = false; delete node.dataset.searchHidden; });
  document.querySelectorAll(".global-search-empty").forEach(node => { node.hidden = true; });
  const input = $("#global-search");
  const meta = pages[currentPage()];
  if (input && meta) { input.placeholder = `搜索${meta[1]}`; input.setAttribute("aria-label", `搜索${meta[1]}`); }
}
$("#global-search")?.addEventListener("input", cleanllmApplyPageSearch);
window.addEventListener("hashchange", () => { cleanllmResetPageSearch(); });
let cleanllmSearchFrame = 0;
const cleanllmContent = document.querySelector(".content-wrap");
if (cleanllmContent) new MutationObserver(() => {
  if (!$("#global-search")?.value.trim() || cleanllmSearchFrame) return;
  cleanllmSearchFrame = requestAnimationFrame(() => {
    cleanllmSearchFrame = 0;
    cleanllmApplyPageSearch();
  });
}).observe(cleanllmContent, {childList: true, subtree: true});
const cleanllmShortcut = document.querySelector(".search kbd");
if (cleanllmShortcut) cleanllmShortcut.textContent = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent) ? "⌘ K" : "Ctrl K";
cleanllmResetPageSearch();
