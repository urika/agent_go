"""Web 操作台单文件前端 SPA（内嵌 HTML，无外部资源依赖）。

拆分自 web_server.py（ISSUE-55）：纯静态模板字符串，不 import 任何 agent_go 模块，
供 web_handler.WebHandler 的 `/` / `/index.html` 路由原样返回。
"""

# ═══════════════════════════════════════════════════════════════
# 单文件前端 SPA（内嵌 HTML，无外部资源依赖）
# ═══════════════════════════════════════════════════════════════

_SPA_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent_go 观察平台</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --border:#2a2f3a; --text:#e6e8eb;
          --dim:#8b93a1; --green:#3fb950; --red:#f85149; --yellow:#d29922;
          --blue:#58a6ff; --purple:#bc8cff; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--text); font-size:14px; }
  header { padding:14px 20px; border-bottom:1px solid var(--border);
           display:flex; align-items:center; gap:16px; }
  header h1 { font-size:16px; margin:0; }
  .status { margin-left:auto; color:var(--dim); }
  .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
           background:var(--panel); border:1px solid var(--border); }
  .container { padding:16px 20px; }
  .filters { display:flex; gap:12px; margin-bottom:12px; align-items:center; flex-wrap:wrap; }
  .filters input[type=text], .filters select {
    background:var(--panel); border:1px solid var(--border); color:var(--text);
    padding:6px 10px; border-radius:6px; font-size:13px; }
  .filter-btn { background:var(--panel); border:1px solid var(--border); color:var(--text);
    padding:5px 10px; border-radius:6px; cursor:pointer; font-size:13px; }
  .filter-btn.active { border-color:var(--blue); color:var(--blue); }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--border);
           vertical-align:top; }
  th { color:var(--dim); font-weight:500; font-size:12px; position:sticky; top:0;
       background:var(--bg); }
  tr.task-row { cursor:pointer; }
  tr.task-row:hover td { background:rgba(88,166,255,0.06); }
  .st-completed { color:var(--green); } .st-failed { color:var(--red); }
  .st-aborted { color:var(--yellow); } .st-blocked { color:var(--red); }
  .st-pending, .st-running { color:var(--dim); }
  .st-cancelled { color:var(--dim); }
  .dim { color:var(--dim); font-size:11px; }
  .task-detail { display:none; }
  .task-detail.open { display:table-row; }
  .detail-box { padding:16px; background:var(--panel); border:1px solid var(--border);
                border-radius:8px; }
  .sub-item { border:1px solid var(--border); border-radius:8px; margin-bottom:8px;
              background:#1a1e26; }
  .sub-head { padding:10px 14px; cursor:pointer; display:flex; gap:10px; align-items:center;
              flex-wrap:wrap; }
  .sub-head .icon { width:18px; }
  .sub-head .title { font-weight:500; }
  .tag { font-size:11px; padding:1px 7px; border-radius:8px; background:var(--border);
         color:var(--dim); }
  .sub-body { display:none; padding:12px 14px; border-top:1px solid var(--border); }
  .sub-body.open { display:block; }
  .tabs { display:flex; gap:4px; margin:10px 0; }
  .tab-btn { background:transparent; border:1px solid var(--border); color:var(--dim);
             padding:4px 12px; border-radius:6px; cursor:pointer; font-size:13px; }
  .tab-btn.active { color:var(--text); border-color:var(--blue); }
  .tab-panel { display:none; }
  .tab-panel.active { display:block; }
  pre, .log { background:#0b0d11; border:1px solid var(--border); border-radius:6px;
              padding:10px; overflow:auto; font-size:12px; line-height:1.5;
              font-family:"SF Mono",Menlo,Consolas,monospace; white-space:pre-wrap; }
  .kv { display:grid; grid-template-columns:150px 1fr; gap:4px 12px; margin-bottom:8px; }
  .kv dt { color:var(--dim); } .kv dd { margin:0; }
  .meter-summary { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:10px; }
  .meter-card { background:#0b0d11; border:1px solid var(--border); border-radius:8px;
                padding:10px 14px; }
  .meter-card .label { color:var(--dim); font-size:12px; }
  .meter-card .val { font-size:16px; font-weight:600; }
  .loading { color:var(--dim); text-align:center; padding:40px; }
  .err { color:var(--red); padding:20px; text-align:center; }
  .kv-table td { padding:4px 8px; font-size:12px; }
  .diff-stats { font-size:12px; color:var(--dim); }
  .vline { width:1px; height:14px; background:var(--border); }
  .nav-tabs { display:flex; gap:4px; margin-left:12px; }
  .nav-tab { background:transparent; border:none; color:var(--dim);
             padding:6px 12px; cursor:pointer; font-size:14px;
             border-bottom:2px solid transparent; }
  .nav-tab:hover { color:var(--text); }
  .nav-tab.active { color:var(--blue); border-bottom-color:var(--blue); }
  .kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
              gap:12px; margin-bottom:20px; }
  .kpi-card { background:var(--panel); border:1px solid var(--border);
              border-radius:8px; padding:14px 18px; }
  .kpi-card .label { color:var(--dim); font-size:12px; margin-bottom:4px; }
  .kpi-card .val { font-size:22px; font-weight:600; }
  .kpi-card .val.green { color:var(--green); }
  .kpi-card .val.red { color:var(--red); }
  .kpi-card .val.yellow { color:var(--yellow); }
  .section-title { font-size:15px; font-weight:600; margin:20px 0 10px;
                   color:var(--text); border-left:3px solid var(--blue);
                   padding-left:10px; }
  .trend-chart { background:#0b0d11; border:1px solid var(--border);
                 border-radius:8px; padding:16px; margin:10px 0; }
  .json-view { background:#0b0d11; border:1px solid var(--border); border-radius:6px;
               padding:10px; overflow:auto; font-size:12px;
               font-family:"SF Mono",Menlo,Consolas,monospace;
               white-space:pre-wrap; max-height:600px; }
  .warn-banner { background:rgba(210,153,34,0.1); border:1px solid var(--yellow);
                 border-radius:8px; padding:10px 14px; margin-bottom:16px;
                 color:var(--yellow); }
  .mode-badge { display:inline-block; padding:4px 14px; border-radius:14px;
                font-size:13px; font-weight:600; }
  .mode-badge.local { background:rgba(63,185,80,0.15); color:var(--green);
                      border:1px solid var(--green); }
  .mode-badge.cloud { background:rgba(88,166,255,0.12); color:var(--blue);
                      border:1px solid var(--blue); }
  .mode-badge.custom { background:rgba(210,153,34,0.12); color:var(--yellow);
                       border:1px solid var(--yellow); }
  .btn { background:var(--panel); border:1px solid var(--border); color:var(--text);
         padding:6px 14px; border-radius:6px; cursor:pointer; font-size:13px; }
  .btn:hover { border-color:var(--blue); }
  .btn.primary { border-color:var(--green); color:var(--green); }
  .btn:disabled { opacity:0.5; cursor:not-allowed; }
  .health-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
                 gap:12px; margin:12px 0; }
  .health-card { background:var(--panel); border:1px solid var(--border);
                 border-radius:8px; padding:12px 14px; font-size:13px; }
  .health-card .role { color:var(--dim); font-size:12px; margin-bottom:4px; }
  .health-card .st-ok { color:var(--green); font-weight:600; }
  .health-card .st-bad { color:var(--red); font-weight:600; }
  .health-card .st-skip { color:var(--dim); }
  .health-card .url { font-size:11px; color:var(--dim); word-break:break-all;
                      margin-top:4px; font-family:Menlo,Consolas,monospace; }
  .run-form { display:flex; gap:8px; align-items:center; margin-bottom:14px;
              background:var(--panel); border:1px solid var(--border);
              border-radius:8px; padding:10px 12px; flex-wrap:wrap; }
  .run-input { background:#0b0d11; border:1px solid var(--border); color:var(--text);
               padding:6px 10px; border-radius:6px; font-size:13px; min-width:120px; }
  .run-textarea { flex:3; resize:vertical; line-height:1.5;
                  font-family:inherit; }
  .run-hint { color:var(--dim); font-size:12px; }
  .op-bar { display:flex; gap:8px; align-items:center; margin:8px 0 12px;
            flex-wrap:wrap; }
  .op-msg { font-size:12px; margin-left:6px; }
  .review-panel { border-top:1px solid var(--border); margin-top:12px;
                  padding-top:4px; }
  .pending-card { background:rgba(210,153,34,0.08); border:1px solid var(--yellow);                  border-radius:8px; padding:12px 14px; margin-bottom:12px; }
  .humility-card { background:rgba(210,153,34,0.06); border:1px solid var(--yellow); border-radius:8px;
                   padding:10px 14px; margin-top:12px; margin-bottom:12px; }
  .humility-card .h-title { font-weight:600; color:var(--yellow); margin-bottom:6px; }
  .humility-card .h-line .abtns { margin-left:8px; white-space:nowrap; }
  .humility-card .abtn { font-size:11px; padding:1px 6px; margin-right:3px; border:1px solid var(--border);
    border-radius:4px; background:transparent; color:var(--fg-dim); cursor:pointer; }
  .humility-card .abtn:hover { border-color:var(--blue); color:var(--blue); }
  .humility-card .abtn.ok:hover { border-color:#3fb950; color:#3fb950; }
  .humility-card .abtn.miss { float:right; }
  .att-done { color:var(--green); font-size:11px; }
  .humility-card .h-line { color:var(--dim); font-size:13px; line-height:1.6; }
  .kanban-toolbar { display:flex; gap:12px; align-items:center; margin-bottom:12px; }
  .kanban-toolbar input { background:var(--panel); border:1px solid var(--border);
                          color:var(--text); padding:6px 10px; border-radius:6px; font-size:13px; }
  .kanban-board { display:flex; gap:12px; align-items:flex-start; overflow-x:auto;
                  padding-bottom:8px; }
  .kanban-col { flex:1 1 0; min-width:220px; background:var(--panel);
                border:1px solid var(--border); border-radius:8px; padding:8px; }
  .kanban-col.drag-over { border-color:var(--blue); }
  .kanban-col-head { display:flex; align-items:center; gap:8px; padding:4px 6px 10px;
                     font-weight:600; }
  .kanban-new-btn { margin-left:auto; background:transparent; border:1px solid var(--border);
                    color:var(--dim); border-radius:6px; cursor:pointer; font-size:12px;
                    padding:2px 8px; }
  .kanban-new-btn:hover { color:var(--blue); border-color:var(--blue); }
  .kanban-card { background:#1a1e26; border:1px solid var(--border); border-radius:8px;
                 padding:10px 12px; margin-bottom:8px; cursor:pointer; }
  .kanban-card:hover { border-color:var(--blue); }
  .kanban-card.archived { opacity:0.55; filter:saturate(0.5); }
  .kanban-card.archived:hover { border-color:var(--yellow); }
  .kanban-card.open { border-color:var(--blue); }
  .kanban-card.selected { border-color:var(--yellow); box-shadow:0 0 0 2px var(--yellow); }
  .kanban-card .kc-title { font-weight:500; margin-bottom:6px; }
  .kanban-card .kc-meta { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
  .kanban-card .kc-foot { display:flex; gap:6px; align-items:center; margin-top:8px; }
  .kanban-move-btn { background:transparent; border:1px solid var(--border); color:var(--dim);
                     border-radius:4px; cursor:pointer; font-size:11px; padding:1px 6px; }
  .kanban-move-btn:hover { color:var(--blue); border-color:var(--blue); }
  .kanban-detail { border-top:1px solid var(--border); margin-top:8px; padding-top:8px; }
  .kanban-detail pre { max-height:240px; }
  .kanban-detail .kc-tasks { margin:8px 0; }
  .kanban-task-link { cursor:pointer; }
  .kanban-task-link:hover { color:var(--blue); border-color:var(--blue); }
  .kanban-history { font-size:12px; color:var(--dim); margin-top:8px; }
  .kanban-history .kh-item { padding:3px 0; border-bottom:1px dashed var(--border); }
  .kanban-form { background:#0b0d11; border:1px solid var(--border); border-radius:8px;
                 padding:10px; margin-bottom:8px; }
  .kanban-form input, .kanban-form select, .kanban-form textarea {
    width:100%; background:var(--panel); border:1px solid var(--border); color:var(--text);
    padding:5px 8px; border-radius:6px; font-size:12px; margin-bottom:6px; }
  .kanban-form textarea { resize:vertical; font-family:inherit; line-height:1.5; }
</style>
</head>
<body>
<header>
  <h1>🌐 agent_go 观察平台</h1>
  <nav class="nav-tabs">
    <button class="nav-tab" data-view="kanban">🗂 看板</button>
    <button class="nav-tab active" data-view="tasks">📋 任务</button>
    <button class="nav-tab" data-view="insight">🧠 洞察</button>
    <button class="nav-tab" data-view="overview">📊 总览</button>
    <button class="nav-tab" data-view="cost">💰 成本</button>
    <button class="nav-tab" data-view="models">🤖 模型</button>
    <button class="nav-tab" data-view="config">⚙️ 配置</button>
    <button class="nav-tab" data-view="storage">💾 运维</button>
    <button class="nav-tab" data-view="archive">🗄️ 归档</button>
  </nav>
  <span class="badge" id="connBadge">连接中…</span>
  <div class="status" id="headerStatus"></div>
</header>
<div class="container">
  <div id="filtersBar" class="filters">
    <input type="text" id="searchInput" placeholder="🔍 搜索任务/ID/描述…">
    <span id="statusFilters"></span>
    <button class="filter-btn" id="refreshBtn">🔄 刷新</button>
  </div>
  <div id="mainView">
    <div class="loading">加载中…</div>
  </div>
</div>

<script>
const STATUS_COLORS = {
  // 新规范状态（status.py TASK_STATES）—— 按生命周期阶段着色
  EXECUTING:'st-running',
  DELIVERY_READY:'st-completed', ACCEPTED_DELIVERY:'st-completed',
  VERIFICATION_FAILED:'st-failed', DELIVERY_FAILED:'st-failed',
  BLOCKED:'st-blocked', CANCELLED:'st-cancelled',
  // 兼容：未知/legacy 状态兜底
  unknown:'st-pending'
};
let tasks = [];
let statusFilter = 'all';
let sse = null;

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
function fmtDur(sec) {
  if (sec === null || sec === undefined) return '—';
  if (sec < 60) return sec.toFixed(1)+'s';
  if (sec < 3600) return (sec/60).toFixed(1)+'m';
  return (sec/3600).toFixed(2)+'h';
}
function fmtCost(c) {
  if (c === null || c === undefined) return '—';
  return '$'+Number(c).toFixed(4);
}

// token 鉴权：服务器启用 --token 时，fetch 带 Authorization 头；
// 401 时提示输入并存 sessionStorage（EventSource 走 ?token= query，见 connectSSE）
let authToken = sessionStorage.getItem('agent_go_token') || '';

async function api(path) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = authToken ? {'Authorization': 'Bearer '+authToken} : {};
    const r = await fetch(path, {headers});
    if (r.status === 401 && attempt === 0) {
      const t = prompt('🔐 服务器启用了 token 鉴权，请输入 token：');
      if (t) { authToken = t; sessionStorage.setItem('agent_go_token', t); continue; }
    }
    if (!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }
  throw new Error('HTTP 401');
}

async function postJSON(path, body) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = {'Content-Type': 'application/json'};
    if (authToken) headers['Authorization'] = 'Bearer '+authToken;
    const r = await fetch(path, {method: 'POST', headers, body: JSON.stringify(body || {})});
    if (r.status === 401 && attempt === 0) {
      const t = prompt('🔐 服务器启用了 token 鉴权，请输入 token：');
      if (t) { authToken = t; sessionStorage.setItem('agent_go_token', t); continue; }
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('HTTP '+r.status));
    return data;
  }
  throw new Error('HTTP 401');
}

function statusIcon(st) {
  return {EXECUTING:'🔄',
          DELIVERY_READY:'🟢', ACCEPTED_DELIVERY:'✅',
          VERIFICATION_FAILED:'🔴', DELIVERY_FAILED:'🔴',
          BLOCKED:'⛔', CANCELLED:'⏹️',
          unknown:'⚪'}[st] || '⚪';
}

// 子任务状态（与任务级状态不同，是执行结果维度）
const SUBTASK_STATUS_COLORS = {
  completed:'st-completed', no_changes:'st-completed', degraded:'st-aborted',
  failed:'st-failed', blocked:'st-blocked', pending:'st-pending'
};
function subtaskStatusIcon(st) {
  return {completed:'🟢', no_changes:'⏭️', degraded:'⚠️',
          failed:'🔴', blocked:'⛔', pending:'⚪'}[st] || '⚪';
}

// ── 任务清单 ────────────────────────────────────────────────
async function loadTasks(endpoint) {
  // endpoint='/api/archive' 时加载历史归档任务，其余默认 '/api/tasks'
  // 两者共享 renderStatusFilters/renderTasks（归档状态已归一化）
  try {
    const data = await api(endpoint || '/api/tasks');
    tasks = data.tasks || [];
    renderStatusFilters();
    renderTasks();
    setConn(true);
  } catch (e) {
    setConn(false);
    document.getElementById('mainView').innerHTML =
      '<div class="err">加载失败: '+esc(e.message)+'</div>';
  }
}

// 状态分组：canonical state → 阶段组（用于聚合筛选）
const STATUS_GROUPS = {
  executing: ['EXECUTING','PAUSED'],
  delivered: ['DELIVERY_READY','ACCEPTED_DELIVERY'],
  failed: ['VERIFICATION_FAILED','DELIVERY_FAILED'],
  blocked: ['BLOCKED'],
  cancelled: ['CANCELLED']
};
const GROUP_LABELS = {
  planning:'📐 规划中', executing:'🔄 执行中', delivered:'🟢 已交付',
  failed:'🔴 失败', blocked:'⛔ 阻断', cancelled:'⏹️ 已取消'
};

function statusGroup(st) {
  for (const [g, states] of Object.entries(STATUS_GROUPS)) {
    if (states.includes(st)) return g;
  }
  return 'executing'; // unknown 兜底：未知状态多为运行中间态，归入执行中
}

function renderStatusFilters() {
  // 按阶段组聚合计数
  const counts = {};
  tasks.forEach(t => {
    const g = statusGroup(t.status);
    counts[g] = (counts[g]||0)+1;
  });
  const order = ['executing','planning','delivered','failed','blocked','cancelled'];
  const html = ['<span class="filter-btn'+(statusFilter==='all'?' active':'')+'" data-s="all">全部 ('+tasks.length+')</span>'];
  // U1：待确认过滤器（有 pending 任务时置最前，高可见）
  const pendingCount = tasks.filter(t => t.pending_confirmation).length;
  if (pendingCount) html.push(
    '<span class="filter-btn'+(statusFilter==='pending-confirm'?' active':'')+'" data-s="pending-confirm" '+
    'style="color:var(--yellow)">🔔 待确认 ('+pendingCount+')</span>');
  order.forEach(g => {
    if (counts[g]) html.push(
      '<span class="filter-btn'+(statusFilter===g?' active':'')+'" data-s="'+g+'">'+
      GROUP_LABELS[g]+' ('+counts[g]+')</span>');
  });
  document.getElementById('statusFilters').innerHTML = html.join('');
  document.querySelectorAll('#statusFilters .filter-btn').forEach(b => {
    b.onclick = () => { statusFilter = b.dataset.s; renderStatusFilters(); renderTasks(); };
  });
  document.getElementById('headerStatus').textContent =
    '共 '+tasks.length+' 个任务（新规范）';
}

function filteredTasks() {
  const q = document.getElementById('searchInput').value.trim().toLowerCase();
  return tasks.filter(t => {
    if (statusFilter === 'pending-confirm') return !!t.pending_confirmation;
    if (statusFilter !== 'all' && statusGroup(t.status) !== statusFilter) return false;
    if (!q) return true;
    return (t.id+' '+t.task+' '+(t.repo||'')).toLowerCase().includes(q);
  });
}

function renderTasks() {
  const list = filteredTasks();
  const runForm =
    '<div class="run-form">'+
    '<input id="runRepo" class="run-input" style="flex:2" placeholder="仓库绝对路径，如 /Users/me/proj">'+
    '<textarea id="runTask" class="run-input run-textarea" rows="3" placeholder="任务描述（自然语言，可多行详细描述需求、验收标准、约束等）"></textarea>'+
    '<select id="runParallel" class="run-input"><option value="1">并发1</option>'+
    '<option value="2">并发2</option><option value="3">并发3</option><option value="4">并发4</option></select>'+
    '<select id="runConfirm" class="run-input">'+
    '<option value="auto">auto（跳过计划确认）</option>'+
    '<option value="web">web（页面确认 Plan）</option></select>'+
    '<select id="runGoal" class="run-input" title="goal 模式：worker 退出前循环跑验证命令直到通过">'+
    '<option value="">goal 默认（policy 判定）</option>'+
    '<option value="on">goal 开（--goal）</option>'+
    '<option value="off">goal 关（--no-goal）</option></select>'+
    '<button class="btn primary" id="btnRunStart">🚀 启动任务</button>'+
    '<span id="runMsg" style="margin-left:8px;font-size:12px"></span>'+
    '</div>';
  if (!list.length) {
    document.getElementById('mainView').innerHTML = runForm +
      '<div class="loading">暂无匹配任务</div>';
    bindRunForm();
    return;
  }
  const rows = list.map(t => {
    const statusCls = STATUS_COLORS[t.status] || 'st-pending';
    return '<tr class="task-row" data-id="'+esc(t.id)+'">'+
      '<td><span class="'+statusCls+'">'+statusIcon(t.status)+' '+esc(t.status)+'</span>'+
      (t.pending_confirmation ? ' <span title="等待计划确认" style="color:var(--yellow)">🔔</span>' : '')+'</td>'+
      '<td>'+esc(t.id)+'</td>'+
      '<td>'+esc(t.task)+'</td>'+
      '<td>'+t.subtask_count+'</td>'+
      '<td>'+t.completed+'/'+t.failed+(t.blocked?'/⛔'+t.blocked:'')+'</td>'+
      '<td>'+fmtCost(t.cost_usd)+'</td>'+
      '<td>'+fmtDur(t.total_elapsed_sec)+'</td>'+
      '</tr>';
  }).join('');
  document.getElementById('mainView').innerHTML = runForm +
    '<table><thead><tr><th>状态</th><th>任务 ID</th><th>描述</th>'+
    '<th>子任务</th><th>完成/失败</th><th>成本</th><th>耗时</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table>';
  document.querySelectorAll('.task-row').forEach(row => {
    row.addEventListener('click', () => toggleTask(row.dataset.id, row));
  });
  bindRunForm();
}

function bindRunForm() {
  const btnRun = document.getElementById('btnRunStart');
  if (!btnRun) return;
  btnRun.onclick = async () => {
    const repo = document.getElementById('runRepo').value.trim();
    const task = document.getElementById('runTask').value.trim();
    const parallel = parseInt(document.getElementById('runParallel').value, 10) || 1;
    const confirmMode = document.getElementById('runConfirm').value;
    const goalVal = document.getElementById('runGoal').value;
    const msg = document.getElementById('runMsg');
    if (!repo || !task) { msg.textContent = '⚠️ 请填写仓库路径和任务描述'; msg.style.color = 'var(--yellow)'; return; }
    btnRun.disabled = true;
    msg.textContent = '启动中（生成 Plan 约需数十秒）…'; msg.style.color = 'var(--dim)';
    try {
      const body = {repo, task, parallel, confirm_mode: confirmMode};
      if (goalVal === 'on') body.goal = true;
      else if (goalVal === 'off') body.goal = false;
      const d = await postJSON('/api/tasks/run', body);
      msg.textContent = '✅ 已启动: '+d.task_id+'（'+d.note+'）';
      msg.style.color = 'var(--green)';
      // U2：web 确认模式 → 自动展开新任务行并滚动定位（用户立即看到确认入口）
      setTimeout(async () => {
        await loadTasks();
        if (confirmMode === 'web') {
          const row = document.querySelector('.task-row[data-id="'+d.task_id+'"]');
          if (row) {
            row.scrollIntoView({block: 'center', behavior: 'smooth'});
            toggleTask(d.task_id, row);
          }
        }
      }, 1200);
    } catch (e) {
      msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)';
    } finally { btnRun.disabled = false; }
  };
}

// ── 任务详情展开 ────────────────────────────────────────────
async function toggleTask(id, row) {
  const existing = document.querySelector('.task-detail[data-id="'+id+'"]');
  if (existing) { existing.remove(); return; }
  const tr = document.createElement('tr');
  tr.className = 'task-detail open';
  tr.dataset.id = id;
  const td = document.createElement('td');
  td.colSpan = 7;
  td.innerHTML = '<div class="detail-box"><div class="loading">加载任务详情…</div></div>';
  tr.appendChild(td);
  row.after(tr);
  try {
    const data = await api('/api/tasks/'+encodeURIComponent(id));
    td.innerHTML = '<div id="pendingCard"></div>' + taskOpsBar(id, data.status, data.managed) +
      renderTaskDetail(data) +
      '<div class="review-panel" id="reviewPanel"><div class="loading">加载审批台…</div></div>' +
      '<div class="review-panel" id="notesPanel"><div class="loading">加载备注…</div></div>';
    bindDetailEvents(id, tr);
    bindTaskOps(id, td);
    loadNotes(id, td);
    loadReviewPanel(id, td);
    loadPendingCard(id, td);
    loadExtraPanels(id, td);
  } catch (e) {
    td.innerHTML = '<div class="detail-box"><div class="err">'+esc(e.message)+'</div></div>';
  }
}

function taskOpsBar(id, status, managed) {
  const running = (status === 'EXECUTING' || status === 'PLANNING');
  // U5：cancel 边界标识——运行中但非本实例托管（CLI 启动/孤儿）→ 禁用 + 明示
  const unmanagedRunning = running && !managed;
  return '<div class="op-bar">'+
    '<button class="btn" data-op="resume" '+(running?'disabled':'')+'>▶️ 恢复</button>'+
    '<button class="btn" data-op="cancel" '+(running && !unmanagedRunning ?'':'disabled')+
      (unmanagedRunning ? ' title="任务非本 web 实例启动（CLI 或孤儿进程），无法用本页取消"' : '')+'>⏹ 取消</button>'+
    '<button class="btn" data-op="clean" '+(running?'disabled':'')+'>🗑 清理</button>'+
    '<button class="btn" data-op="report">📄 报告</button>'+
    (unmanagedRunning ? '<span class="tag" style="color:var(--yellow)">👁 外部进程，仅可观测（CLI: agent_go resume/cancel）</span>' : '')+
    '<span class="op-msg" id="opMsg"></span>'+
    '</div>';
}

function bindTaskOps(id, td) {
  const msg = td.querySelector('#opMsg');
  const say = (t, color) => { msg.textContent = t; msg.style.color = color || 'var(--dim)'; };
  td.querySelectorAll('[data-op]').forEach(btn => {
    btn.onclick = async () => {
      const op = btn.dataset.op;
      if (op === 'report') {
        window.open('/api/tasks/'+encodeURIComponent(id)+'/report?format=html', '_blank');
        return;
      }
      if (op === 'resume' && !confirm('恢复任务 '+id+'？（从断点续跑剩余子任务）')) return;
      if (op === 'cancel' && !confirm('取消任务 '+id+'？\\n将发送 SIGINT（与 Ctrl+C 同义），pipeline 收尾后停止。')) return;
      if (op === 'clean' && !confirm('清理任务 '+id+'？\\n将删除任务数据目录（worktree/tag 一并清理），不可恢复！')) return;
      btn.disabled = true;
      say(op+' 执行中…');
      try {
        let d;
        if (op === 'clean') d = await delJSON('/api/tasks/'+encodeURIComponent(id), {confirm: true});
        else d = await postJSON('/api/tasks/'+encodeURIComponent(id)+'/'+op, {});
        say('✅ '+(d.note || d.status || (d.removed ? '已清理 '+d.removed.length+' 个目录' : '完成')), 'var(--green)');
        setTimeout(loadTasks, 1200);
      } catch (e) {
        say('❌ '+e.message, 'var(--red)');
        btn.disabled = false;
      }
    };
  });
}

async function loadReviewPanel(id, td) {
  const panel = td.querySelector('#reviewPanel');
  let rv = {};
  try { rv = await api('/api/tasks/'+encodeURIComponent(id)+'/review'); } catch (e) {}
  const decision = rv.decision;
  const decBadge = decision ?
    {'approved':'<span style="color:var(--green)">✅ 已通过</span>',
     'rejected':'<span style="color:var(--red)">❌ 已拒绝</span>',
     'changes-requested':'<span style="color:var(--yellow)">📝 需修改</span>'}[decision] || esc(decision)
    : '<span style="color:var(--dim)">未审批</span>';
  panel.innerHTML =
    '<div class="section-title">⚖️ 交付审批台</div>'+
    '<div class="op-bar"><span>当前决策: '+decBadge+'</span><span class="vline"></span>'+
    '<button class="btn" data-rv="review">🔍 聚合审查</button>'+
    '<button class="btn" data-rv="deep">🔬 深层审查</button><span class="vline"></span>'+
    '<button class="btn" data-rv="approve">✅ 通过</button>'+
    '<button class="btn" data-rv="reject">❌ 拒绝</button>'+
    '<button class="btn" data-rv="changes">📝 需修改</button><span class="vline"></span>'+
    '<button class="btn primary" data-rv="merge">🔀 Merge</button>'+
    '<button class="btn" data-rv="pr">🚀 PR</button>'+
    '<span class="op-msg" id="rvMsg"></span></div>';
  const msg = panel.querySelector('#rvMsg');
  const say = (t, color) => { msg.textContent = t; msg.style.color = color || 'var(--dim)'; };
  panel.querySelectorAll('[data-rv]').forEach(btn => {
    btn.onclick = async () => {
      const op = btn.dataset.rv;
      btn.disabled = true;
      say('执行中…');
      try {
        if (op === 'review' || op === 'deep') {
          const d = await postJSON('/api/tasks/'+encodeURIComponent(id)+'/review',
                                   op === 'deep' ? {deep: true} : {});
          say('✅ '+(d.status === 'review_started' ? '深层审查已启动（后台运行，完成后刷新查看 review.json）' : '审查完成'), 'var(--green)');
        } else if (op === 'approve' || op === 'reject' || op === 'changes') {
          const decision = op === 'changes' ? 'changes-requested' : op;
          let comment = '';
          if (decision !== 'approve') {
            comment = prompt('审批意见（可选）：') || '';
          }
          if (!confirm('确认决策「'+decision+'」？将写入 review.json 并记录审计。')) { btn.disabled = false; say(''); return; }
          await postJSON('/api/tasks/'+encodeURIComponent(id)+'/review/decision', {decision, comment});
          say('✅ 决策已记录: '+decision, 'var(--green)');
          setTimeout(() => loadReviewPanel(id, td), 800);
        } else if (op === 'merge') {
          const pv = await api('/api/tasks/'+encodeURIComponent(id)+'/merge-preview');
          if (pv.pr_url) { say('⚠️ 已走 PR 交付路径（'+pv.pr_url+'），merge 互斥', 'var(--yellow)'); btn.disabled = false; return; }
          if (pv.explicit_merge_commit) { say('✅ 已合并过: '+pv.explicit_merge_commit.slice(0,12), 'var(--green)'); btn.disabled = false; return; }
          if (pv.mergeable === false) {
            say('❌ 无法 clean merge'+((pv.conflicts||[]).length ? '，冲突: '+pv.conflicts.join(', ') : (pv.error ? ': '+pv.error : '')), 'var(--red)');
            btn.disabled = false; return;
          }
          const text = '确认合并？\\n\\n  delivery 分支: '+pv.delivery_branch+
            '\\n  目标分支: '+pv.target_branch+'\\n  新增 commit: '+(pv.ahead != null ? pv.ahead : '?')+
            '\\n\\n确定后选择是否推送 remote。';
          if (!confirm(text)) { btn.disabled = false; say(''); return; }
          const push = confirm('合并成功。是否推送到 remote（origin）？\\n确定=推送，取消=仅本地合并');
          const d = await postJSON('/api/tasks/'+encodeURIComponent(id)+'/merge', {push, remote: 'origin'});
          say('✅ merge 完成'+(push ? '（已推送）' : ''), 'var(--green)');
          setTimeout(() => loadReviewPanel(id, td), 800);
        } else if (op === 'pr') {
          const push = confirm('创建 PR？\\n确定=推送分支并创建真实 PR\\n取消=仅生成 PR.md（offline 预览）');
          const d = await postJSON('/api/tasks/'+encodeURIComponent(id)+'/pr', {push, remote: 'origin'});
          if (d.pr_url) say('✅ PR 已创建: '+d.pr_url, 'var(--green)');
          else say('✅ '+(push ? 'PR 完成' : 'PR.md 已生成（offline）'), 'var(--green)');
          setTimeout(() => loadReviewPanel(id, td), 800);
        }
      } catch (e) {
        say('❌ '+e.message, 'var(--red)');
      } finally {
        btn.disabled = false;
      }
    };
  });
}

async function loadNotes(id, td) {
  const panel = td.querySelector('#notesPanel');
  if (!panel) return;
  let d;
  try { d = await api('/api/tasks/'+encodeURIComponent(id)+'/notes'); }
  catch (e) { panel.innerHTML = '<div class="err">'+esc(e.message)+'</div>'; return; }
  const notes = (d.notes || []);
  const rows = notes.slice().reverse().map(n =>
    '<div style="border-bottom:1px solid var(--border);padding:6px 0">'+
    '<div style="font-size:11px;color:var(--dim)">'+esc(n.author)+' · '+esc((n.ts||'').replace('T',' ').slice(0,19))+'</div>'+
    '<div style="white-space:pre-wrap">'+esc(n.text)+'</div></div>').join('');
  panel.innerHTML =
    '<div class="section-title">📝 任务备注（协作）</div>'+
    '<div class="run-form" style="margin-bottom:8px">'+
    '<input id="noteInput" class="run-input" style="flex:3" placeholder="添加备注（所有协作者可见）…">'+
    '<button class="btn" id="noteSend">发送</button></div>'+
    (notes.length ? '<div style="max-height:260px;overflow:auto">'+rows+'</div>' : '<div style="color:var(--dim);font-size:12px">暂无备注</div>');
  const send = panel.querySelector('#noteSend');
  const input = panel.querySelector('#noteInput');
  send.onclick = async () => {
    const text = input.value.trim();
    if (!text) return;
    send.disabled = true;
    try {
      await postJSON('/api/tasks/'+encodeURIComponent(id)+'/notes', {text});
      input.value = '';
      loadNotes(id, td);
    } catch (e) {
      alert('❌ '+e.message);
      send.disabled = false;
    }
  };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') send.onclick(); });
}

async function loadExtraPanels(id, td) {
  // R12 偏差 + R17 worktree 折叠面板（审批台下方）
  const anchor = td.querySelector('#reviewPanel');
  if (!anchor) return;
  const box = document.createElement('div');
  box.innerHTML = '<div id="devPanel"></div><div id="wtPanel"></div>';
  anchor.after(box);
  try {
    const dv = await api('/api/tasks/'+encodeURIComponent(id)+'/deviation');
    if (dv.total > 0) {
      const typeRows = Object.entries(dv.by_type || {}).map(([k, v]) =>
        '<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>').join('');
      const causeRows = Object.entries(dv.by_root_cause || {}).map(([k, v]) =>
        '<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>').join('');
      const evRows = (dv.events || []).slice(-10).map(e =>
        '<tr><td>'+esc(e.deviation_type||'')+'</td><td>'+esc(e.root_cause_category||'')+'</td>'+
        '<td>'+esc((e.summary||'').slice(0,80))+'</td></tr>').join('');
      box.querySelector('#devPanel').innerHTML =
        '<div class="section-title">📐 偏差记录（'+dv.total+'）</div>'+
        '<div style="display:flex;gap:24px;flex-wrap:wrap">'+
        '<table><thead><tr><th>类型</th><th>数</th></tr></thead><tbody>'+typeRows+'</tbody></table>'+
        '<table><thead><tr><th>根因</th><th>数</th></tr></thead><tbody>'+causeRows+'</tbody></table></div>'+
        (evRows ? '<table style="margin-top:8px"><thead><tr><th>类型</th><th>根因</th><th>摘要</th></tr></thead><tbody>'+evRows+'</tbody></table>' : '');
    }
  } catch (e) {}
  try {
    const wt = await api('/api/tasks/'+encodeURIComponent(id)+'/worktrees');
    if ((wt.worktrees || []).length) {
      const rows = wt.worktrees.map(w =>
        '<tr><td>'+esc(w.subtask_id)+'</td><td>'+esc(w.status)+'</td>'+
        '<td style="font-family:Menlo,monospace;font-size:11px">'+esc(w.branch)+'</td>'+
        '<td>'+(w.preserved ? '📌 保留' : '')+'</td>'+
        '<td style="font-size:11px;color:var(--dim)">'+esc((w.failure_reason||'').slice(0,60))+'</td></tr>').join('');
      box.querySelector('#wtPanel').innerHTML =
        '<div class="section-title">🌳 保留 Worktree（'+wt.worktrees.length+'）</div>'+
        '<table><thead><tr><th>子任务</th><th>状态</th><th>分支</th><th></th><th>失败原因</th></tr></thead>'+
        '<tbody>'+rows+'</tbody></table>';
    }
  } catch (e) {}
}

async function loadPendingCard(id, td) {
  const slot = td.querySelector('#pendingCard');
  if (!slot) return;
  // U3：详情行存活期间每 5s 轮询（Plan 生成 30-60s，pending 出现后自动渲染卡片；
  // 决策提交后 pending 消失/下一级 pending 出现均靠轮询驱动视图更新）
  if (!slot.dataset.polling) {
    slot.dataset.polling = '1';
    const poll = () => {
      if (!document.body.contains(slot)) return;  // 详情已关闭 → 停止
      loadPendingCard(id, td);
    };
    setTimeout(poll, 5000);
  }
  let d;
  try { d = await api('/api/tasks/'+encodeURIComponent(id)+'/pending-confirmation'); }
  catch (e) { return; }
  if (!d.pending) { slot.innerHTML = ''; return; }
  const p = d.pending;
  const age = Math.max(0, Math.round((Date.now() - new Date(p.ts).getTime()) / 60000));
  const left = Math.max(1, Math.round(p.timeout_sec / 60) - age);
  let body = '';
  if (p.stage === 'plan') {
    const plan = p.payload || {};
    const steps = (plan.steps || []).map((s, i) =>
      '<tr><td>'+(i+1)+'</td><td>'+esc(s.title || s.name || '')+'</td>'+
      '<td>'+esc(s.difficulty || '')+'</td><td>'+esc(s.agent_type || '')+'</td></tr>').join('');
    body = '<div style="font-weight:600;margin-bottom:6px">'+esc(plan.title || '执行计划')+'</div>'+
      (plan.summary ? '<div style="color:var(--dim);margin-bottom:8px">'+esc(plan.summary)+'</div>' : '')+
      '<table><thead><tr><th>#</th><th>步骤</th><th>难度</th><th>角色</th></tr></thead><tbody>'+steps+'</tbody></table>';
  } else {
    const subs = (p.payload && p.payload.subtasks) || [];
    body = '<div style="font-weight:600;margin-bottom:6px">子任务拆解（'+subs.length+' 个）</div>'+
      '<table><thead><tr><th>ID</th><th>标题</th><th>难度</th></tr></thead><tbody>'+
      subs.map(s => '<tr><td>'+esc(s.id||'')+'</td><td>'+esc(s.title||'')+'</td><td>'+esc(s.difficulty||'')+'</td></tr>').join('')+
      '</tbody></table>';
  }
  const btns = p.stage === 'plan'
    ? '<button class="btn primary" data-cf="Y">✅ 确认执行</button>'+
      '<button class="btn" data-cf="R">🔄 重新生成</button>'+
      '<button class="btn" data-cf="N">❌ 取消任务</button>'
    : '<button class="btn primary" data-cf="Y">✅ 确认子任务</button>'+
      '<button class="btn" data-cf="N">❌ 取消任务</button>';
  slot.innerHTML =
    '<div class="pending-card">'+
    '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">'+
    '<span style="font-size:16px">🔔</span>'+
    '<span style="font-weight:600">等待确认：'+(p.stage === 'plan' ? '执行计划' : '子任务拆解')+'</span>'+
    '<span style="color:var(--yellow);font-size:12px">约 '+left+' 分钟后超时自动取消</span></div>'+
    body+
    '<div class="op-bar" style="margin-top:10px">'+btns+'<span class="op-msg" id="cfMsg"></span></div>'+
    '</div>';
  slot.querySelectorAll('[data-cf]').forEach(btn => {
    btn.onclick = async () => {
      const decision = btn.dataset.cf;
      const msg = slot.querySelector('#cfMsg');
      if (decision === 'N' && !confirm('取消任务 '+id+'？')) return;
      btn.disabled = true;
      try {
        await postJSON('/api/tasks/'+encodeURIComponent(id)+'/confirm', {stage: p.stage, decision});
        msg.textContent = '✅ 已提交决策: '+decision; msg.style.color = 'var(--green)';
        setTimeout(() => loadPendingCard(id, td), 3000);
      } catch (e) {
        msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)';
        btn.disabled = false;
      }
    };
  });
}

async function delJSON(path, body) {
  const headers = {'Content-Type': 'application/json'};
  if (authToken) headers['Authorization'] = 'Bearer '+authToken;
  const r = await fetch(path, {method: 'DELETE', headers, body: JSON.stringify(body || {})});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('HTTP '+r.status));
  return data;
}

function renderTaskDetail(d) {
  const items = (d.subtasks||[]).map((s,i) => {
    const statusCls = SUBTASK_STATUS_COLORS[s.status] || 'st-pending';
    const src = s.agent_type_source || 'default';
    return '<div class="sub-item">'+
      '<div class="sub-head" data-sub="'+esc(s.id)+'">'+
        '<span class="icon '+statusCls+'">'+subtaskStatusIcon(s.status)+'</span>'+
        '<span class="title">['+esc(s.id)+'] '+esc(s.title)+'</span>'+
        '<span class="tag">'+esc(s.difficulty||'medium')+'</span>'+
        '<span class="tag">'+esc(s.agent_type||'developer')+'</span>'+
        '<span class="tag">'+esc(src)+'</span>'+
        (s.retry_count? '<span class="tag" style="color:var(--yellow)">retry ×'+s.retry_count+'</span>':'')+
        '<span class="tag">'+fmtDur(s.duration_sec)+'</span>'+
        (s.verify_ok!==undefined? '<span class="tag">verify:'+ (s.verify_ok?'✅':'❌')+'</span>':'')+
      '</div>'+
      '<div class="sub-body" id="sub-body-'+esc(s.id)+'">'+
        '<div class="loading">点击子任务查看明细</div>'+
      '</div></div>';
  }).join('');
  // 谦逊层盲区卡片（#51：交底报告进操作台 + P1.5 归因四按钮）
  const bs = d.blind_spots || {};
  const att = ((d.blind_spot_attributions || {}).items) || {};
  const attState = (k) => att[k] ? ' <span class="att-done">[已注:' + att[k].attribution + ']</span>' : '';
  const abtns = (sig, key) =>
    ' <span class="abtns">' +
    '<button class="abtn ok" data-item="' + sig + ':' + key + '" data-att="confirmed" title="确认命中：交付后人工修复验证了该盲区">✓</button>' +
    '<button class="abtn" data-item="' + sig + ':' + key + '" data-att="false-hit" title="假阳性：自动计命中但实为巧合触碰">假阳</button>' +
    '<button class="abtn" data-item="' + sig + ':' + key + '" data-att="false-clear" title="假阴性：盲区真出了问题（提前判定命中）">假阴</button></span>';
  const sigLines = (label, sig) => ((bs[sig] && bs[sig].length)
    ? bs[sig].map(k => '<div class="h-line">' + label + ': ' + esc(String(k)) + attState(sig + ':' + k) + abtns(sig, k) + '</div>').join('')
    : '');
  const persp = d.uncovered_perspectives || [];
  const layer = d.layer_attribution || {};
  const blindLines = [];
  blindLines.push(sigLines('未覆盖验收 ID', 'uncovered_acceptance_ids'));
  blindLines.push(sigLines('弱锚定验证子任务', 'weakly_anchored_subtasks'));
  if (bs.unattributed_failures && bs.unattributed_failures.length) blindLines.push('<div class="h-line">无根因失败: ' + bs.unattributed_failures.map(esc).join(', ') + '</div>');
  if (bs.baseline_dirty) blindLines.push('<div class="h-line">任务启动时工作区有未提交改动</div>');
  blindLines.push(sigLines('语义评估不确定', 'inconclusive_evaluations'));
  persp.forEach(p => blindLines.push('<div class="h-line">未覆盖视角 [' + esc(p.perspective||'') + ']: ' + esc(p.reason||'') + '</div>'));
  if (layer.primary) blindLines.push('<div class="h-line">层间归因: ' + esc(layer.primary) + '</div>');
  const missDone = (d.blind_spot_attributions || {}).task_level ? ' <span class="att-done">[已注:missed]</span>' : '';
  const humilityHtml = blindLines.filter(Boolean).length
    ? '<div class="humility-card"><div class="h-title">⚠️ 已知盲区（系统主动交底）' + missDone +
      '<button class="abtn miss" data-item="" data-att="missed" title="漏报：交付后出问题但当时无任何盲区标注">漏报复注</button></div>' +
      blindLines.filter(Boolean).join('') + '</div>'
    : '';
  return '<div class="kv">'+
    '<dt>任务</dt><dd>'+esc(d.task)+'</dd>'+
    '<dt>仓库</dt><dd>'+esc(d.repo)+'</dd>'+
    '<dt>状态</dt><dd><span class="'+((STATUS_COLORS[d.status])||'')+'">'+statusIcon(d.status)+' '+esc(d.status)+'</span></dd>'+
    '<dt>创建时间</dt><dd>'+esc(d.created_at||'')+'</dd>'+
    '</div>'+humilityHtml+'<div style="margin-top:12px">'+items+'</div>';
}

function bindDetailEvents(taskId, tr) {
  tr.querySelectorAll('.abtn').forEach(btn => {
    btn.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      const item = btn.dataset.item, a = btn.dataset.att;
      const note = (a === 'missed') ? (prompt('漏报复注：简述交付后出了什么问题（可空）') || '') : (prompt('归因备注（可空，≤200 字）') || '');
      try {
        await postJSON('/api/tasks/' + encodeURIComponent(taskId) + '/blind-spot-attribution',
          {item: item, attribution: a, note: note});
        const span = document.createElement('span');
        span.className = 'att-done';
        span.textContent = ' [已注:' + a + ']';
        if (item) { const grp = btn.closest('.abtns'); if (grp) { grp.before(span); grp.remove(); } }
        else { btn.replaceWith(span); }
      } catch (e) { alert('标注失败: ' + e.message); }
    });
  });
  tr.querySelectorAll('.sub-head').forEach(head => {
    head.addEventListener('click', async () => {
      const subId = head.dataset.sub;
      const body = document.getElementById('sub-body-'+subId);
      const isOpen = body.classList.contains('open');
      if (isOpen) { body.classList.remove('open'); return; }
      if (!body.dataset.loaded) {
        body.innerHTML = '<div class="loading">加载子任务明细…</div>';
        try {
          const detail = await api('/api/tasks/'+encodeURIComponent(taskId)+'/'+encodeURIComponent(subId)+'/detail');
          body.innerHTML = renderSubDetail(detail);
          bindSubTabs(body, taskId, subId);
          body.dataset.loaded = '1';
        } catch (e) {
          body.innerHTML = '<div class="err">'+esc(e.message)+'</div>';
        }
      }
      body.classList.add('open');
    });
  });
}

function renderSubDetail(d) {
  const r = d.result || {};
  const stats = r.change_stats || {};
  const statsHtml = Object.keys(stats).length ? '<pre>'+esc(JSON.stringify(stats, null, 2))+'</pre>'
    : '<span class="diff-stats">无改动统计</span>';
  return '<div class="tabs">'+
    '<button class="tab-btn active" data-tab="overview">概览</button>'+
    '<button class="tab-btn" data-tab="verify">验证</button>'+
    '<button class="tab-btn" data-tab="log">日志</button>'+
    '<button class="tab-btn" data-tab="metering">计量</button>'+
    '<button class="tab-btn" data-tab="timeline">时间线</button>'+
    '</div>'+
    '<div class="tab-panel active" data-panel="overview">'+
      '<div class="kv">'+
      '<dt>描述</dt><dd>'+esc(d.description||'')+'</dd>'+
      '<dt>依赖</dt><dd>'+(Array.isArray(d.depends_on)&&d.depends_on.length?d.depends_on.map(esc).join(', '):'—')+'</dd>'+
      '<dt>文件</dt><dd>'+(Array.isArray(d.files_hint)&&d.files_hint.length?d.files_hint.map(esc).join(', '):'—')+'</dd>'+
      '<dt>技能</dt><dd>'+(Array.isArray(d.skills)&&d.skills.length?d.skills.map(esc).join(', '):'—')+'</dd>'+
      '<dt>Agent</dt><dd>'+esc(d.agent_type||'developer')+'（'+esc(r.agent_type_source||'default')+'）</dd>'+
      '<dt>状态</dt><dd>'+esc(r.status||'—')+'</dd>'+
      '<dt>耗时</dt><dd>'+fmtDur(r.duration_sec)+'</dd>'+
      '<dt>重试</dt><dd>'+(r.retry_count??'—')+'</dd>'+
      '<dt>验证</dt><dd>'+(r.verify_ok===true?'✅ 通过':r.verify_ok===false?'❌ 失败':'—')+'</dd>'+
      '<dt>退出码</dt><dd>'+(r.exit_code??'—')+'</dd>'+
      '<dt>沙箱</dt><dd>'+esc(r.sandbox_type||'—')+'</dd>'+
      '<dt>失败原因</dt><dd>'+esc(r.failure_reason||'—')+'</dd>'+
      '<dt>工作树</dt><dd>'+esc(r.worktree||'—')+'</dd>'+
      '<dt>摘要</dt><dd>'+esc(r.summary||'—')+'</dd>'+
      '</div>'+
      '<div class="kv"><dt>改动统计</dt><dd></dd></div>'+
      statsHtml+
    '</div>'+
    '<div class="tab-panel" data-panel="verify">'+
      renderVerify(r.verification_results||[])+
    '</div>'+
    '<div class="tab-panel" data-panel="log"><div class="loading">加载日志…</div></div>'+
    '<div class="tab-panel" data-panel="metering"><div class="loading">加载计量…</div></div>'+
    '<div class="tab-panel" data-panel="timeline"><div class="loading">加载时间线…</div></div>';
}

function renderVerify(vrs) {
  if (!vrs.length) return '<div class="kv"><dt>无验证结果</dt><dd></dd></div>';
  return '<table class="kv-table"><thead><tr><th>命令</th><th>类型</th><th>结果</th><th>耗时</th></tr></thead><tbody>'+
    vrs.map(v => '<tr><td>'+esc(v.command||v.desc||'')+'</td>'+
      '<td>'+esc(v.type||'shell')+'</td>'+
      '<td>'+(v.passed===true?'<span class="st-completed">✅ 通过</span>':v.passed===false?'<span class="st-failed">❌ 失败</span>':'—')+'</td>'+
      '<td>'+fmtDur(v.duration_sec)+'</td></tr>').join('')+
    '</tbody></table>';
}

function bindSubTabs(body, taskId, subId) {
  const panels = { overview:null, verify:null, log:null, metering:null, timeline:null };
  body.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      body.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      body.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const name = btn.dataset.tab;
      body.querySelector('.tab-panel[data-panel="'+name+'"]').classList.add('active');
      const t = name;
      if (t === 'log' && !panels.log) {
        panels.log = api('/api/tasks/'+encodeURIComponent(taskId)+'/'+encodeURIComponent(subId)+'/log')
          .then(d => { body.querySelector('[data-panel="log"]').innerHTML = renderLog(d.lines||[]); });
      } else if (t === 'metering' && !panels.metering) {
        panels.metering = api('/api/tasks/'+encodeURIComponent(taskId)+'/metering')
          .then(d => { body.querySelector('[data-panel="metering"]').innerHTML = renderMetering(d); });
      } else if (t === 'timeline' && !panels.timeline) {
        panels.timeline = api('/api/tasks/'+encodeURIComponent(taskId)+'/replay')
          .then(d => { body.querySelector('[data-panel="timeline"]').innerHTML = renderTimeline(d); });
      }
    });
  });
}

function renderLog(lines) {
  if (!lines.length) return '<div class="kv"><dt>无日志</dt><dd></dd></div>';
  return '<pre>'+lines.map(l => esc(l.text)).join('\\n')+'</pre>';
}

function renderMetering(d) {
  if (!d || !d.summary) return '<div class="kv"><dt>无计量数据</dt><dd></dd></div>';
  const cards = Object.entries(d.summary).map(([role, s]) =>
    '<div class="meter-card"><div class="label">'+esc(role)+'</div>'+
    '<div class="val">$'+s.cost_usd+'</div>'+
    '<div>'+s.count+' 次调用 · '+s.prompt_tokens+'→'+s.completion_tokens+' tokens</div>'+
    '<div>延迟 '+s.latency_ms+'ms</div></div>').join('');
  const rows = (d.rows||[]).map(r => {
    // 模型列：actual_model；若 routed_model 不同则标注路由别名→实际后端
    let modelCell = esc(r.actual_model||r.virtual_model||'');
    const rm = r.routed_model||'';
    if (rm && rm !== (r.actual_model||'')) {
      modelCell += '<br><span class="dim">'+esc(rm)+' →</span>';
    }
    return '<tr><td>'+esc(r.subtask_id||'')+'</td>'+
    '<td>'+esc(r.role)+'</td><td>'+modelCell+'</td>'+
    '<td>'+r.prompt_tokens+'</td><td>'+r.completion_tokens+'</td>'+
    '<td>$'+r.cost_usd+'</td><td>'+r.latency_ms+'ms</td>'+
    '<td>'+esc(r.result||'')+'</td></tr>';
  }).join('');
  return '<div class="meter-summary">'+cards+'</div>'+
    (rows? '<table class="kv-table"><thead><tr><th>子任务</th><th>角色</th><th>模型</th>'+
    '<th>prompt</th><th>completion</th><th>成本</th><th>延迟</th><th>结果</th></tr></thead><tbody>'+rows+'</tbody></table>':'');
}

function renderTimeline(d) {
  const rows = (d.timeline||[]).map(ev => {
    const st = ev.type || '';
    return '<tr><td>'+esc(ev.ts||'')+'</td><td>'+esc(st)+'</td><td>'+esc(ev.label||ev.detail||'')+'</td></tr>';
  }).join('');
  return rows? '<table class="kv-table"><thead><tr><th>时间</th><th>类型</th><th>详情</th></tr></thead><tbody>'+rows+'</tbody></table>'
    : '<div class="kv"><dt>无时间线</dt><dd></dd></div>';
}

// ── 看板（Kanban）──────────────────────────────────────
let kanbanData = null;
let kanbanRepoFilter = '';
let kanbanExpanded = null;      // 展开详情的卡片 id
let kanbanEditing = null;       // 正在编辑的卡片 id
let kanbanNewCardStage = null;  // 正在新建卡片的列
let kanbanShowArchived = false; // 归档视图开关（含已归档卡片，可取消归档）
let kanbanSelected = null;      // 键盘操作选中的卡片 id

async function loadKanban() {
  try {
    const [kb, stats, cq] = await Promise.all([
      api('/api/kanban' + (kanbanShowArchived ? '?archived=1' : '')),
      api('/api/kanban/classification-stats'),
      api('/api/kanban/cost-quality'),
    ]);
    kanbanData = kb;
    kanbanData._stats = stats;
    kanbanData._costQuality = cq;
    renderKanban();
    setConn(true);
  } catch (e) {
    setConn(false);
    document.getElementById('mainView').innerHTML =
      '<div class="err">加载失败: '+esc(e.message)+'</div>';
  }
}

function renderKanban() {
  const d = kanbanData || {stages: [], cards: {}, card_types: {}};
  const filter = kanbanRepoFilter.trim().toLowerCase();
  let html = '<div class="kanban-toolbar">'+
    '<input type="text" id="kanbanRepoFilter" placeholder="🔍 按 repo 筛选…" value="'+esc(kanbanRepoFilter)+'">'+
    '<button class="btn '+(kanbanShowArchived?'primary':'')+'" id="kanbanArchToggle" title="显示/隐藏已归档卡片">🗂 '+
    (kanbanShowArchived?'已归档（含）':'已归档')+'</button>'+
    '<span class="dim">共 '+(d.total||0)+' 张卡片'+(filter?'（已筛选）':'')+'</span>'+
    '<span class="dim" style="margin-left:auto;font-size:11px;color:var(--dim)" title="选中卡片后可用：↑/↓ 或 j/k 移动选中 · Enter/e 编辑 · Space 展开/收起 · ←/→ 或 [/] 流转 · ⌘⌫/Delete 删除 · A 归档 · U 取消归档 · D 派发 · Esc 取消选中">'+
    '⌨ 单击选中卡片后可用键盘（双击编辑 · ⌘⌫ 删除）</span></div>';
  // W4.1 分类器自学习：分类准确率面板（auto/manual/pending 完成率）
  const stats = d._stats;
  if (stats && stats.by_automation) {
    const label = {auto: '🤖 自动', manual: '👤 人工', pending: '⏳ 待判定'};
    const badges = Object.entries(stats.by_automation).map(([k, s]) => {
      const rate = (s.pass_rate != null) ? (s.pass_rate * 100).toFixed(0) + '%' : '-';
      return '<span class="tag" title="完成 '+(s.completed||0)+'/'+s.total+'">'+label[k]+' '+s.total+'（'+rate+'）</span>';
    }).join(' ');
    html += '<div class="op-bar" style="margin:6px 0 10px;font-size:12px;color:var(--dim)">分类准确率（自学习反馈）: '+badges+'</div>';
  }
  // W4.2 成本-质量自适应：本地队列 vs 云端权衡面板
  const cq = d._costQuality;
  if (cq && cq.groups) {
    const rows = Object.entries(cq.groups).filter(([_, g]) => g.tasks > 0).map(([mode, g]) => {
      const rate = (g.pass_rate != null) ? (g.pass_rate * 100).toFixed(0) + '%' : '-';
      const cpp = (g.cost_per_pass != null) ? '$' + g.cost_per_pass.toFixed(4) : '-';
      return '<span class="tag" title="任务 '+g.tasks+'，完成 '+g.completed+'">'+mode+': '+g.tasks+' 任务 / 通过率 '+rate+' / $/pass '+cpp+'</span>';
    }).join(' ');
    if (rows) {
      html += '<div class="op-bar" style="margin:0 0 10px;font-size:12px;color:var(--dim)">成本-质量权衡（W4.2 自适应）: '+rows+
        (cq.suggestion ? ' <span style="color:var(--yellow)">💡 '+esc(cq.suggestion)+'</span>' : '')+'</div>';
    }
  }
  html += '<div class="kanban-board">';
  d.stages.forEach((st, si) => {
    let cards = d.cards[st.key] || [];
    if (filter) cards = cards.filter(c => (c.repo||'').toLowerCase().includes(filter));
    html += '<div class="kanban-col" data-stage="'+st.key+'">'+
      '<div class="kanban-col-head"><span>'+esc(st.label)+'</span>'+
      '<span class="tag">'+cards.length+'</span>'+
      '<button class="kanban-new-btn" data-stage="'+st.key+'">＋ 新建</button></div>';
    if (kanbanNewCardStage === st.key) html += kanbanFormHtml('new', {});
    cards.forEach(c => { html += kanbanCardHtml(c, si, d.stages.length); });
    html += '</div>';
  });
  html += '</div>';
  document.getElementById('mainView').innerHTML = html;
  bindKanbanEvents();
}

function findKanbanCard(id) {
  const cards = (kanbanData||{}).cards || {};
  for (const key of Object.keys(cards)) {
    const hit = (cards[key]||[]).find(c => c.id === id);
    if (hit) return hit;
  }
  return null;
}

function kanbanCardHtml(c, stageIdx, stageCount) {
  const typeLabel = ((kanbanData||{}).card_types||{})[c.type] || c.type;
  const repoShort = c.repo ? c.repo.split('/').filter(Boolean).pop() : '';
  let html = '<div class="kanban-card'+(c.archived?' archived':'')+(kanbanExpanded===c.id?' open':'')+(kanbanSelected===c.id?' selected':'')+'"'+(c.archived?'':' draggable="true"')+' data-card="'+esc(c.id)+'">'+
    '<div class="kc-title">'+esc(c.title)+'</div>'+
    '<div class="kc-meta"><span class="tag">'+esc(typeLabel)+'</span>';
  if (c.archived) html += '<span class="tag" style="color:var(--yellow)">🗂 已归档</span>';
  // W1.2：automation 分类徽标（看板任务编排）
  const auto = c.automation || 'pending';
  const autoBadge = {auto:['🤖 自动','var(--green)'], manual:['👤 人工','var(--yellow)'], pending:['⏳ 待判定','var(--dim)']}[auto];
  if (autoBadge) html += '<span class="tag" style="color:'+autoBadge[1]+'" title="自动化分类">'+autoBadge[0]+'</span>';
  if (repoShort) html += '<span class="tag" title="'+esc(c.repo)+'">📁 '+esc(repoShort)+'</span>';
  if (c.cron) html += '<span class="tag">⏰ '+esc(c.cron)+'</span>';
  if (c.latest_task) html += '<span class="badge '+(STATUS_COLORS[c.latest_task.status]||'st-pending')+'">'+esc(c.latest_task.status)+'</span>';
  html += '</div>';
  // 流转按钮（◀▶ 无拖拽 fallback）
  html += '<div class="kc-foot">';
  if (!c.archived && stageIdx > 0) html += '<button class="kanban-move-btn" data-card="'+esc(c.id)+'" data-dir="-1" title="移到上一阶段">◀</button>';
  if (!c.archived && stageIdx < stageCount-1) html += '<button class="kanban-move-btn" data-card="'+esc(c.id)+'" data-dir="1" title="移到下一阶段">▶</button>';
  html += '<span class="dim" style="margin-left:auto">'+esc((c.updated||'').replace('T',' ').slice(5,16))+'</span></div>';
  if (kanbanExpanded === c.id) html += kanbanDetailHtml(c);
  html += '</div>';
  return html;
}

function kanbanDetailHtml(c) {
  let html = '<div class="kanban-detail">';
  if (kanbanEditing === c.id) {
    html += kanbanFormHtml('edit', c);
  } else if (c.description) {
    // MVP 不做 markdown 渲染，pre 原文展示
    html += '<pre>'+esc(c.description)+'</pre>';
  } else {
    html += '<div class="dim">（无描述）</div>';
  }
  if ((c.task_ids||[]).length) {
    html += '<div class="kc-tasks">'+c.task_ids.map(tid =>
      '<span class="tag kanban-task-link" data-task="'+esc(tid)+'" title="点击查看任务列表">🔗 '+esc(tid)+'</span>').join(' ')+'</div>';
  }
  html += '<div class="op-bar">'+
    '<button class="btn kanban-op" data-op="edit" data-card="'+esc(c.id)+'">✏️ 编辑</button>';
  if (!c.archived && (c.type === 'implementation' || c.type === 'periodic')) {
    // 幂等防护前端侧：最新任务运行中 → 禁派发
    const lt = c.latest_task || {};
    const running = (lt.status === 'EXECUTING' || lt.status === 'PLANNING');
    html += '<button class="btn primary kanban-op" data-op="dispatch" data-card="'+esc(c.id)+'"'+
      (running ? ' disabled title="该卡片已有运行中任务（'+esc(lt.status)+'），不可重复派发"' : '') +
      '>🚀 派发执行</button>';
  }
  if (c.archived)
    html += '<button class="btn kanban-op" data-op="unarchive" data-card="'+esc(c.id)+'">♻️ 恢复（取消归档）</button>';
  else
    html += '<button class="btn kanban-op" data-op="archive" data-card="'+esc(c.id)+'">🗄️ 归档</button>';
  if (!(c.task_ids||[]).length)
    html += '<button class="btn kanban-op" data-op="delete" data-card="'+esc(c.id)+'">🗑️ 删除</button>';
  // W4.3 降级建议：有任务且非完成状态（失败/进行中）→ 提供降级/修复建议
  if ((c.task_ids||[]).length && c.stage !== 'operations')
    html += '<button class="btn kanban-op" data-op="suggest-degrade" data-card="'+esc(c.id)+'">🤖 降级建议</button>';
  // W3.3：operations 列审批按钮（approve→approved；reject/changes-requested→rejected+回退 implementation）
  if (c.stage === 'operations') {
    const ap = c.approval || 'pending';
    if (ap === 'pending') {
      html += '<span class="vline"></span>'+
        '<button class="btn primary kanban-op" data-op="review-approve" data-card="'+esc(c.id)+'">✅ 通过</button>'+
        '<button class="btn kanban-op" data-op="review-reject" data-card="'+esc(c.id)+'">❌ 拒绝</button>'+
        '<button class="btn kanban-op" data-op="review-changes" data-card="'+esc(c.id)+'">📝 需修改</button>';
    } else {
      html += '<span class="tag" style="color:'+(ap==='approved'?'var(--green)':'var(--red)')+'">'+
        (ap==='approved'?'✅ 已通过':'❌ 已拒绝（已回退 implementation）')+'</span>';
    }
  }
  html += '<span class="op-msg" id="kanbanMsg-'+esc(c.id)+'"></span></div>';
  const hist = (c.history||[]).slice().reverse();
  if (hist.length) {
    html += '<div class="kanban-history">'+hist.map(h =>
      '<div class="kh-item">'+esc((h.ts||'').replace('T',' '))+' · '+esc(h.action)+
      (h.from?' '+esc(h.from)+' → '+esc(h.to||''):'')+
      (h.note?' · '+esc(h.note):'')+'</div>').join('')+'</div>';
  }
  html += '</div>';
  return html;
}

function kanbanFormHtml(mode, c) {
  const isNew = mode === 'new';
  const types = (kanbanData||{}).card_types || {};
  let html = '<div class="kanban-form" data-mode="'+mode+'" data-card="'+esc(c.id||'')+'">';
  html += '<input type="text" class="kf-title" placeholder="标题 *" value="'+esc(c.title||'')+'">';
  if (isNew) {
    html += '<select class="kf-type">'+
      Object.entries(types).map(([k, v]) =>
        '<option value="'+esc(k)+'">'+esc(v)+'</option>').join('')+'</select>';
  }
  html += '<input type="text" class="kf-repo" placeholder="repo 路径（实施/周期类必填）" value="'+esc(c.repo||'')+'">';
  html += '<textarea class="kf-desc" rows="3" placeholder="描述（markdown，讨论沉淀）">'+esc(c.description||'')+'</textarea>';
  if (isNew || c.type === 'periodic')
    html += '<input type="text" class="kf-cron" placeholder="cron 表达式（周期类展示用）" value="'+esc(c.cron||'')+'">';
  html += '<div style="display:flex;gap:8px;align-items:center">'+
    '<button class="btn primary kf-save">'+(isNew?'创建':'保存')+'</button>'+
    '<button class="btn kf-cancel">取消</button>'+
    '<span class="op-msg kf-msg"></span></div></div>';
  return html;
}

function bindKanbanEvents() {
  const main = document.getElementById('mainView');
  // repo 筛选（客户端过滤）
  const fi = document.getElementById('kanbanRepoFilter');
  if (fi) {
    fi.oninput = () => { kanbanRepoFilter = fi.value; renderKanban(); };
    if (kanbanRepoFilter) { fi.focus(); fi.setSelectionRange(fi.value.length, fi.value.length); }
  }
  // 归档视图开关
  const archBtn = document.getElementById('kanbanArchToggle');
  if (archBtn) {
    archBtn.onclick = () => {
      kanbanShowArchived = !kanbanShowArchived;
      kanbanExpanded = null;
      loadKanban();
    };
  }
  // 列头新建按钮（内联表单开关）
  main.querySelectorAll('.kanban-new-btn').forEach(b => {
    b.onclick = () => {
      kanbanNewCardStage = (kanbanNewCardStage === b.dataset.stage) ? null : b.dataset.stage;
      renderKanban();
    };
  });
  // 卡片：单击选中 + 展开/收起详情，双击进入编辑，拖拽流转
  main.querySelectorAll('.kanban-card').forEach(el => {
    el.addEventListener('click', ev => {
      if (ev.target.closest('button') || ev.target.closest('.kanban-form')) return;
      const id = el.dataset.card;
      kanbanSelected = id;
      kanbanExpanded = (kanbanExpanded === id) ? null : id;
      kanbanEditing = null;
      renderKanban();
    });
    el.addEventListener('dblclick', ev => {
      if (ev.target.closest('button') || ev.target.closest('.kanban-form')) return;
      const id = el.dataset.card;
      kanbanSelected = id;
      kanbanExpanded = id;
      kanbanEditing = id;
      renderKanban();
    });
    el.addEventListener('dragstart', ev => {
      ev.dataTransfer.setData('text/plain', el.dataset.card);
      ev.dataTransfer.effectAllowed = 'move';
    });
  });
  // 列：拖放目标
  main.querySelectorAll('.kanban-col').forEach(col => {
    col.addEventListener('dragover', ev => { ev.preventDefault(); col.classList.add('drag-over'); });
    col.addEventListener('dragleave', () => col.classList.remove('drag-over'));
    col.addEventListener('drop', async ev => {
      ev.preventDefault();
      col.classList.remove('drag-over');
      const cardId = ev.dataTransfer.getData('text/plain');
      const stage = col.dataset.stage;
      if (!cardId || !stage) return;
      try {
        await postJSON('/api/kanban/cards/'+encodeURIComponent(cardId)+'/move', {stage});
        loadKanban();
      } catch (e) { alert('流转失败: '+e.message); }
    });
  });
  // ◀▶ 流转按钮（无拖拽 fallback）
  main.querySelectorAll('.kanban-move-btn').forEach(b => {
    b.onclick = async ev => {
      ev.stopPropagation();
      const stages = (kanbanData||{}).stages || [];
      const card = findKanbanCard(b.dataset.card);
      if (!card) return;
      const idx = stages.findIndex(s => s.key === card.stage);
      const ni = idx + parseInt(b.dataset.dir, 10);
      if (ni < 0 || ni >= stages.length) return;
      try {
        await postJSON('/api/kanban/cards/'+encodeURIComponent(card.id)+'/move', {stage: stages[ni].key});
        loadKanban();
      } catch (e) { alert('流转失败: '+e.message); }
    };
  });
  // 新建/编辑表单
  main.querySelectorAll('.kanban-form').forEach(f => {
    const msg = f.querySelector('.kf-msg');
    f.querySelector('.kf-cancel').onclick = () => {
      if (f.dataset.mode === 'new') kanbanNewCardStage = null; else kanbanEditing = null;
      renderKanban();
    };
    f.querySelector('.kf-save').onclick = async () => {
      const title = f.querySelector('.kf-title').value.trim();
      const repo = f.querySelector('.kf-repo').value.trim();
      const description = f.querySelector('.kf-desc').value;
      const cronEl = f.querySelector('.kf-cron');
      const typeEl = f.querySelector('.kf-type');
      if (!title) { msg.textContent = '⚠️ 标题必填'; msg.style.color = 'var(--yellow)'; return; }
      const type = typeEl ? typeEl.value : '';
      // 前端预校验（后端仍会再校验一次）
      if (typeEl && (type === 'implementation' || type === 'periodic') && !repo) {
        msg.textContent = '⚠️ 实施/周期类卡片必须填 repo'; msg.style.color = 'var(--yellow)'; return;
      }
      try {
        if (f.dataset.mode === 'new') {
          await postJSON('/api/kanban/cards', {title, type, stage: kanbanNewCardStage,
            repo, description, cron: cronEl ? cronEl.value.trim() : ''});
          kanbanNewCardStage = null;
        } else {
          const body = {title, repo, description};
          if (cronEl) body.cron = cronEl.value.trim();
          await postJSON('/api/kanban/cards/'+encodeURIComponent(f.dataset.card)+'/update', body);
          kanbanEditing = null;
        }
        loadKanban();
      } catch (e) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; }
    };
  });
  // 详情操作按钮
  main.querySelectorAll('.kanban-op').forEach(b => {
    b.onclick = async ev => {
      ev.stopPropagation();
      const id = b.dataset.card;
      const op = b.dataset.op;
      const msg = document.getElementById('kanbanMsg-'+id);
      if (op === 'edit') { kanbanEditing = id; renderKanban(); return; }
      if (op === 'dispatch') {
        if (!confirm('派发卡片到 agent_go 执行？\\n任务文本 = 卡片标题 + 描述')) return;
        b.disabled = true;
        try {
          const d = await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/dispatch', {parallel: 1});
          if (msg) { msg.textContent = '✅ 已派发: '+d.task_id; msg.style.color = 'var(--green)'; }
          setTimeout(loadKanban, 800);
        } catch (e) {
          if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; }
          b.disabled = false;
        }
        return;
      }
      if (op === 'archive') {
        if (!confirm('归档该卡片？（归档后不在看板展示）')) return;
        try {
          await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/archive', {archived: true});
          kanbanExpanded = null;
          loadKanban();
        } catch (e) { if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; } }
        return;
      }
      if (op === 'unarchive') {
        try {
          await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/archive', {archived: false});
          kanbanExpanded = null;
          loadKanban();
        } catch (e) { if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; } }
        return;
      }
      if (op === 'delete') {
        if (!confirm('物理删除该卡片？仅未派发过任务的卡片可删除。')) return;
        try {
          await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/delete', {});
          kanbanExpanded = null;
          loadKanban();
        } catch (e) { if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; } }
        return;
      }
      // W4.3 降级建议：insight 分析失败原因 → 显示建议
      if (op === 'suggest-degrade') {
        b.disabled = true;
        if (msg) { msg.textContent = '🤖 分析中…'; msg.style.color = 'var(--dim)'; }
        try {
          const d = await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/suggest-degrade', {});
          if (msg) {
            const tips = (d.suggestions||[]).map(s => '• '+s.action).join('\\n');
            msg.textContent = '🤖 降级建议:\\n'+tips;
            msg.style.color = 'var(--yellow)';
            msg.style.whiteSpace = 'pre-wrap';
          }
        } catch (e) {
          if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; }
          b.disabled = false;
        }
        return;
      }
      // W3.3：operations 列审批
      if (op.startsWith('review-')) {
        const decision = op.replace('review-', '');
        const labels = {'approve':'通过','reject':'拒绝','changes-requested':'需修改'};
        if (!confirm('审批决策「'+labels[decision]+'」？'+('reject'===decision||'changes-requested'===decision?'将回退 implementation 列重做。':'将标记为已通过。'))) return;
        b.disabled = true;
        try {
          const d = await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/review', {decision});
          if (msg) { msg.textContent = '✅ 审批已记录: '+decision; msg.style.color = 'var(--green)'; }
          setTimeout(loadKanban, 800);
        } catch (e) {
          if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; }
          b.disabled = false;
        }
        return;
      }
    };
  });
  // 关联任务 → 跳任务列表
  main.querySelectorAll('.kanban-task-link').forEach(el => {
    el.onclick = ev => { ev.stopPropagation(); switchView('tasks'); };
  });
}

// 看板键盘操作（绑定一次，避免 renderKanban 重复累加）：
//   选中卡片后可用常用键操作——↑/↓ 或 j/k 移动选中，Enter/e 编辑，Space 展开/收起，
//   ←/→ 或 [/] 流转阶段，⌘⌫/⌘Delete/Delete 删除，A 归档，U 取消归档，D 派发，Esc 取消选中。
function bindKanbanKeyboard() {
  if (bindKanbanKeyboard._bound) return;
  bindKanbanKeyboard._bound = true;
  document.addEventListener('keydown', e => {
    if (currentView !== 'kanban') return;
    if (!kanbanSelected) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'))
      return;
    const card = findKanbanCard(kanbanSelected);
    if (!card) { kanbanSelected = null; return; }
    const key = e.key;
    // 编辑（Enter 或 e）
    if (key === 'Enter' || key === 'e' || key === 'E') {
      e.preventDefault();
      kanbanExpanded = card.id;
      kanbanEditing = card.id;
      renderKanban();
      const f = document.querySelector('.kanban-form .kf-title');
      if (f) setTimeout(() => f.focus(), 0);
      return;
    }
    // 展开/收起（Space）
    if (key === ' ') {
      e.preventDefault();
      kanbanExpanded = (kanbanExpanded === card.id) ? null : card.id;
      kanbanEditing = null;
      renderKanban();
      return;
    }
    // 流转阶段（←/→ 或 [/]）
    if (key === 'ArrowLeft' || key === '[') {
      e.preventDefault();
      moveKanbanCardBy(card, -1);
      return;
    }
    if (key === 'ArrowRight' || key === ']') {
      e.preventDefault();
      moveKanbanCardBy(card, 1);
      return;
    }
    // 删除（⌘⌫ / ⌘Delete / Ctrl+⌫ / Delete）
    if ((e.metaKey || e.ctrlKey) && (key === 'Backspace' || key === 'Delete')) {
      e.preventDefault();
      deleteKanbanCard(card);
      return;
    }
    if (key === 'Delete') {
      e.preventDefault();
      deleteKanbanCard(card);
      return;
    }
    // 归档 / 取消归档（a / u）
    if (key === 'a' || key === 'A') {
      e.preventDefault();
      archiveKanbanCard(card, true);
      return;
    }
    if (key === 'u' || key === 'U') {
      e.preventDefault();
      archiveKanbanCard(card, false);
      return;
    }
    // 派发（d，仅 implementation/periodic 且未运行）
    if (key === 'd' || key === 'D') {
      e.preventDefault();
      dispatchKanbanCard(card);
      return;
    }
    // 移动选中（↑/↓ 或 j/k）
    if (key === 'ArrowDown' || key === 'j' || key === 'J') {
      e.preventDefault();
      moveKanbanSelection(card, 1);
      return;
    }
    if (key === 'ArrowUp' || key === 'k' || key === 'K') {
      e.preventDefault();
      moveKanbanSelection(card, -1);
      return;
    }
    // 取消选中（Esc）
    if (key === 'Escape') {
      e.preventDefault();
      kanbanSelected = null;
      kanbanEditing = null;
      renderKanban();
      return;
    }
  });
}

// 看板键盘辅助：按可见顺序取卡片列表（跨列，受 repo 筛选/归档开关约束）
function kanbanVisibleCards() {
  const d = kanbanData || {stages: [], cards: {}};
  const filter = kanbanRepoFilter.trim().toLowerCase();
  const out = [];
  (d.stages || []).forEach(st => {
    (d.cards[st.key] || []).forEach(c => {
      if (kanbanShowArchived ? true : !c.archived) {
        if (filter && !(c.repo || '').toLowerCase().includes(filter)) return;
        out.push(c);
      }
    });
  });
  return out;
}

function moveKanbanSelection(card, dir) {
  const list = kanbanVisibleCards();
  const idx = list.findIndex(c => c.id === card.id);
  if (idx < 0) return;
  const ni = idx + dir;
  if (ni < 0 || ni >= list.length) return;
  kanbanSelected = list[ni].id;
  renderKanban();
}

function moveKanbanCardBy(card, dir) {
  const stages = (kanbanData || {}).stages || [];
  const idx = stages.findIndex(s => s.key === card.stage);
  const ni = idx + dir;
  if (ni < 0 || ni >= stages.length) return;
  if (card.archived) return;
  postJSON('/api/kanban/cards/'+encodeURIComponent(card.id)+'/move', {stage: stages[ni].key})
    .then(() => { kanbanSelected = card.id; loadKanban(); })
    .catch(err => alert('流转失败: '+err.message));
}

function deleteKanbanCard(card) {
  if ((card.task_ids || []).length) {
    alert('该卡片已关联任务，不能删除（可归档）。');
    return;
  }
  if (!confirm('物理删除该卡片「'+card.title+'」？仅未派发过任务的卡片可删除。')) return;
  postJSON('/api/kanban/cards/'+encodeURIComponent(card.id)+'/delete', {})
    .then(() => {
      kanbanSelected = null;
      kanbanExpanded = null;
      loadKanban();
    })
    .catch(err => alert('删除失败: '+err.message));
}

function archiveKanbanCard(card, archived) {
  if (archived && !confirm('归档该卡片「'+card.title+'」？（归档后不在看板展示）')) return;
  postJSON('/api/kanban/cards/'+encodeURIComponent(card.id)+'/archive', {archived: !!archived})
    .then(() => {
      if (archived) kanbanSelected = null;
      kanbanExpanded = null;
      loadKanban();
    })
    .catch(err => alert((archived?'归档':'取消归档')+'失败: '+err.message));
}

function dispatchKanbanCard(card) {
  if (card.archived || (card.type !== 'implementation' && card.type !== 'periodic')) {
    alert('仅实施/周期类未归档卡片可派发。');
    return;
  }
  const lt = card.latest_task || {};
  if (lt.status === 'EXECUTING' || lt.status === 'PLANNING') {
    alert('该卡片已有运行中任务（'+lt.status+'），不可重复派发。');
    return;
  }
  if (!confirm('派发卡片「'+card.title+'」到 agent_go 执行？\\n任务文本 = 卡片标题 + 描述')) return;
  postJSON('/api/kanban/cards/'+encodeURIComponent(card.id)+'/dispatch', {parallel: 1})
    .then(() => { setTimeout(loadKanban, 800); })
    .catch(err => alert('派发失败: '+err.message));
}

// ── 视图切换 + 新视图渲染（P0-2 / P1 / P2）─────────────────
let currentView = 'tasks';

function switchView(name) {
  currentView = name;
  document.querySelectorAll('.nav-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.view === name);
  });
  // 任务视图和归档视图都显示 filters（归档复用任务渲染）
  document.getElementById('filtersBar').style.display =
    (name === 'tasks' || name === 'archive') ? '' : 'none';
  const main = document.getElementById('mainView');
  main.innerHTML = '<div class="loading">加载中…</div>';
  // 返回 loader 的 Promise，调用方可链式等待渲染完成
  if (name === 'tasks') return loadTasks();
  if (name === 'kanban') return loadKanban();
  if (name === 'insight') return loadInsight();
  if (name === 'archive') return loadTasks('/api/archive');
  if (name === 'overview') return loadOverview();
  if (name === 'cost') return loadCost();
  if (name === 'models') return loadModels();
  if (name === 'config') return loadConfig();
  if (name === 'storage') return loadStorage();
  return Promise.resolve();
}

async function loadOverview() {
  const d = await api('/api/overview');
  const k = d.kpi || {};
  const dpr = d.dollar_per_pass_rate;
  // KPI 卡片
  let html = '<div class="kpi-grid">'+
    kpiCard('任务总数', k.total||0, 'blue')+
    kpiCard('进行中', k.in_progress||0, k.in_progress>0?'yellow':'')+
    kpiCard('已交付', k.delivered||0, 'green')+
    kpiCard('失败', k.failed||0, k.failed>0?'red':'')+
    kpiCard('今日交付', k.today_delivered||0, 'green')+
    kpiCard('今日成本', fmtCost(k.today_cost||0), '')+
    kpiCard('$/pass rate', dpr!=null?('$'+Number(dpr).toFixed(4)):'—',
            dpr!=null&&dpr>0.05?'red':'green')+
    '</div>';
  // 7 天成本趋势（SVG 柱状图）
  html += '<div class="section-title">📈 近 7 天成本趋势</div>';
  html += renderTrendChart(d.cost_trend_7d || []);
  document.getElementById('mainView').innerHTML = html;
}

function kpiCard(label, val, color) {
  const cls = color ? ' '+color : '';
  return '<div class="kpi-card"><div class="label">'+esc(label)+'</div>'+
    '<div class="val'+cls+'">'+esc(val)+'</div></div>';
}

function renderTrendChart(days) {
  if (!days.length) return '<div class="kv"><dt>无数据</dt><dd></dd></div>';
  const maxCost = Math.max(...days.map(d => d.cost), 0.01);
  const w = 60, h = 120, pad = 30;
  const totalW = days.length * (w + 10) + pad * 2;
  const bars = days.map((d, i) => {
    const barH = maxCost > 0 ? (d.cost / maxCost) * h : 0;
    const x = pad + i * (w + 10);
    const y = pad + h - barH;
    return '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+barH+'" fill="var(--blue)" rx="3">'+
      '<title>'+esc(d.date)+': $'+Number(d.cost).toFixed(4)+'</title></rect>'+
      '<text x="'+(x+w/2)+'" y="'+(pad+h+18)+'" text-anchor="middle" fill="var(--dim)" font-size="11">'+esc(d.date.slice(5))+'</text>'+
      '<text x="'+(x+w/2)+'" y="'+(y-5)+'" text-anchor="middle" fill="var(--text)" font-size="11">$'+Number(d.cost).toFixed(2)+'</text>';
  }).join('');
  return '<div class="trend-chart"><svg width="'+totalW+'" height="'+(pad*2+h+30)+'">'+
    bars+'</svg></div>';
}

async function loadCost() {
  const d = await api('/api/cost');
  let html = '<div class="section-title">💵 全局成本总览</div>';
  html += '<div class="kpi-grid">'+
    kpiCard('总成本', fmtCost(d.total_cost||0), 'blue')+
    '</div>';
  // by_model
  html += '<div class="section-title">按模型分解</div>';
  const modelRows = (d.by_model||[]).map(m => {
    // 模型名标注三态，区分「路由别名」「直连真模型」「未解析回退」
    let nameCell = esc(m.name);
    if (m.routed_model && m.routed_model !== m.name) {
      // 路由别名：实际后端 ≠ 路由名（如 deepseek-v4-flash 的路由名是 claude-haiku-4-5）
      nameCell += '<br><span class="dim">路由别名 '+esc(m.routed_model)+' →</span>';
    } else if (!m.routed_model && m.resolved !== false) {
      // 直连：无路由名，actual_model 是真实后端（如真调 Claude API，非别名）
      nameCell += '<br><span class="dim">直连（非路由别名）</span>';
    }
    if (m.resolved === false) {
      nameCell += ' <span title="未解析出真实后端模型，显示的是路由别名">⚠️</span>';
    }
    return '<tr><td>'+nameCell+'</td><td>'+fmtCost(m.cost)+'</td><td>'+m.pct+'%</td>'+
    '<td>'+m.calls+'</td><td>'+m.prompt_tokens+'</td><td>'+m.completion_tokens+'</td></tr>';
  }).join('');
  html += '<table><thead><tr><th>模型</th><th>成本</th><th>占比</th>'+
    '<th>调用数</th><th>prompt tokens</th><th>completion tokens</th></tr></thead><tbody>'+modelRows+'</tbody></table>';
  // by_role
  html += '<div class="section-title">按角色分解</div>';
  const roleRows = (d.by_role||[]).map(r =>
    '<tr><td>'+esc(r.name)+'</td><td>'+fmtCost(r.cost)+'</td><td>'+r.pct+'%</td><td>'+r.calls+'</td></tr>'
  ).join('');
  html += '<table><thead><tr><th>角色</th><th>成本</th><th>占比</th><th>调用数</th></tr></thead><tbody>'+roleRows+'</tbody></table>';
  // Top N 任务
  html += '<div class="section-title">💸 成本最高的 20 个任务</div>';
  const topRows = (d.top_tasks||[]).map((t,i) =>
    '<tr class="task-row" data-id="'+esc(t.task_id)+'">'+
    '<td>'+(i+1)+'</td><td>'+esc(t.task_id)+'</td><td>'+fmtCost(t.cost)+'</td></tr>'
  ).join('');
  html += '<table><thead><tr><th>#</th><th>任务</th><th>成本</th></tr></thead><tbody>'+topRows+'</tbody></table>';
  // R13 本地 TCO 面板（D1：显著标注估算）
  try {
    const tco = await api('/api/local-tco');
    if (tco.total_calls > 0) {
      const rows = (tco.by_model || []).map(r =>
        '<tr><td>'+esc(r.model)+'</td><td>'+r.calls+'</td>'+
        '<td>$'+r.unit_cost.toFixed(4)+'</td><td>$'+r.tco_usd.toFixed(4)+'</td>'+
        '<td>'+(r.configured ? '' : '<span style="color:var(--yellow)">未配置</span>')+'</td></tr>').join('');
      html += '<div class="section-title">🔌 本地模型 TCO（估算成本）</div>'+
        '<div class="warn-banner">⚠️ 以下为按 local_model_cost 单价 × 调用次数的<b>估算成本</b>，非真实账单。</div>'+
        '<div class="kpi-grid">'+
        kpiCard('本地调用总数', tco.total_calls, '')+
        kpiCard('估算总成本', '$'+tco.total_tco_usd.toFixed(4), 'yellow')+
        '</div>'+
        '<table><thead><tr><th>模型</th><th>调用数</th><th>单价/次</th><th>估算成本</th><th></th></tr></thead>'+
        '<tbody>'+rows+'</tbody></table>'+
        (tco.note ? '<div style="color:var(--dim);font-size:12px;margin-top:6px">'+esc(tco.note)+'</div>' : '');
    }
  } catch (e) {}
  document.getElementById('mainView').innerHTML = html;
  // 复用任务详情展开：跳转到任务视图并定位到该任务（等待加载完成再填搜索框）
  document.querySelectorAll('#mainView .task-row').forEach(row => {
    row.addEventListener('click', () => {
      switchView('tasks').then(() => {
        document.getElementById('searchInput').value = row.dataset.id;
        renderTasks();
      });
    });
  });
}

async function loadModels() {
  const d = await api('/api/models');
  let html = '<div class="section-title">🏭 生产环境模型成本（实际任务 metering）</div>';
  const prodRows = (d.production||[]).map(m => {
    let nameCell = esc(m.model);
    if (m.routed_model && m.routed_model !== m.model) {
      nameCell += '<br><span class="dim">路由别名 '+esc(m.routed_model)+' →</span>';
    } else if (!m.routed_model && m.resolved !== false) {
      nameCell += '<br><span class="dim">直连（非路由别名）</span>';
    }
    if (m.resolved === false) {
      nameCell += ' <span title="未解析出真实后端">⚠️</span>';
    }
    return '<tr><td>'+nameCell+'</td><td>'+fmtCost(m.cost)+'</td>'+
    '<td>'+m.calls+'</td><td>'+m.task_count+'</td>'+
    '<td>$'+Number(m.avg_cost_per_call||0).toFixed(6)+'</td></tr>';
  }).join('') || '<tr><td colspan="5">无数据</td></tr>';
  html += '<table><thead><tr><th>模型</th><th>总成本</th><th>调用数</th>'+
    '<th>任务数</th><th>avg $/call</th></tr></thead><tbody>'+prodRows+'</tbody></table>';
  // bench 对比
  html += '<div class="section-title">🧪 Bench 模型决策矩阵（实验数据）</div>';
  if (d.bench && d.bench.length) {
    const benchRows = d.bench.map(m =>
      '<tr><td>'+esc(m.model)+'</td><td>'+m.sample_size+'</td>'+
      '<td>'+((m.avg_pass_rate||0)*100).toFixed(1)+'%</td>'+
      '<td>'+fmtCost(m.avg_cost_usd||0)+'</td>'+
      '<td>$'+Number(m.dollar_per_pass||0).toFixed(4)+'</td>'+
      '<td>'+esc(m.recommendation||'—')+'</td></tr>'
    ).join('');
    html += '<table><thead><tr><th>模型</th><th>样本</th><th>通过率</th>'+
      '<th>avg 成本</th><th>$/pass</th><th>建议</th></tr></thead><tbody>'+benchRows+'</tbody></table>';
  } else {
    html += '<div class="kv"><dt>无 bench 数据</dt><dd>（运行 agent_go eval bench 生成）</dd></div>';
  }
  document.getElementById('mainView').innerHTML = html;
}

async function loadConfig() {
  const [d, prof, health] = await Promise.all([
    api('/api/config'), api('/api/profiles'), api('/api/health'),
  ]);
  let html = '<div class="section-title">🎛️ 配置中心</div>';
  // 模式徽标 + 切换按钮
  const modeLabel = {local: '🟢 纯本地模式', cloud: '☁️ 云端模式', custom: '🔧 自定义: '+esc(prof.current)};
  html += '<div style="display:flex;align-items:center;gap:14px;margin-bottom:12px">'+
    '<span class="mode-badge '+esc(prof.mode)+'">'+(modeLabel[prof.mode] || esc(prof.mode))+'</span>'+
    (prof.mode !== 'local' ? '<button class="btn primary" id="btnLocal">⚡ 一键本地</button>' : '')+
    (prof.mode !== 'cloud' ? '<button class="btn" id="btnCloud">☁️ 恢复云端</button>' : '')+
    '</div>';
  // 健康面板
  html += '<div class="health-grid">';
  for (const role of ['plan', 'worker', 'evaluator', 'local_proxy']) {
    const h = health[role] || {};
    let st, cls;
    if (h.skipped) { st = '⏭ '+(h.reason || '跳过'); cls = 'st-skip'; }
    else if (h.ok) { st = '✅ 可达'+(h.latency_ms != null ? ' · '+h.latency_ms+'ms' : ''); cls = 'st-ok'; }
    else { st = '❌ '+(h.error || '不可达'); cls = 'st-bad'; }
    html += '<div class="health-card"><div class="role">'+esc(role)+'</div>'+
      '<div class="'+cls+'">'+esc(st)+'</div>'+
      (h.model ? '<div>模型: '+esc(h.model)+'</div>' : '')+
      (h.url ? '<div class="url">'+esc(h.url)+'</div>' : '')+
      '</div>';
  }
  html += '</div>';
  if (health.mismatch) {
    html += '<div class="warn-banner">⚠️ '+esc(health.suggestion || '本地代理模型与 profile 不一致')+
            ' <button class="btn primary" id="btnLocalFix">重新生成 local profile</button></div>';
  }
  // R9 消费：代理路由策略可视（模型→后端路由偏好/云端模型/智能路由阈值）
  try {
    const pp = await api('/api/proxy-policies');
    if (pp.ok) {
      html += '<div class="section-title">🛣️ 代理路由策略（'+esc(pp.proxy_url)+'）</div>';
      html += '<div class="kpi-grid">'+
        kpiCard('智能路由', pp.route_enabled ? '✅ 启用' : '⏸ 关闭', pp.route_enabled ? 'green' : '')+
        kpiCard('云转阈值', pp.threshold_chars != null ? (pp.threshold_chars/1000)+'K chars' : '-', '')+
        kpiCard('云端模型', esc(pp.cloud_model || '-'), '')+
        kpiCard('云端 Key', pp.cloud_key_set ? '✅ 已配置' : '❌ 未配置', pp.cloud_key_set ? 'green' : 'red')+
        '</div>';
      // 模型路由偏好表
      const prefRows = Object.entries(pp.preferences || {}).map(([m, p]) => {
        const behavior = (p.behavior||'prefer') + (p.route_bias ? '·'+p.route_bias : '');
        return '<tr><td>'+esc(m)+'</td><td>'+esc(behavior)+'</td>'+
          '<td>'+esc(p.cloud_model || '-')+'</td>'+
          '<td>'+(p.threshold_factor ? '×'+p.threshold_factor : '-')+'</td></tr>';
      }).join('');
      if (prefRows) html += '<table><thead><tr><th>模型</th><th>偏好</th><th>云端模型</th><th>阈值系数</th></tr></thead><tbody>'+prefRows+'</tbody></table>';
      // 后端 providers
      const provs = Object.entries(pp.providers || {}).map(([k, v]) =>
        '<tr><td>'+esc(k)+'</td><td>'+esc(v.base_url || '')+'</td>'+
        '<td>'+(v.key_set ? '✅' : '❌')+'</td></tr>').join('');
      if (provs) html += '<table style="margin-top:8px"><thead><tr><th>Provider</th><th>Base URL</th><th>Key</th></tr></thead><tbody>'+provs+'</tbody></table>';
    } else {
      html += '<div class="section-title">🛣️ 代理路由策略</div><div style="color:var(--dim);font-size:12px">代理不可达或未提供 R9 接口（'+esc(pp.error||'')+'）</div>';
    }
  } catch (e) {}
  // profile 列表（非备份）
  const userProfiles = (prof.profiles || []).filter(p => !p.is_backup);
  if (userProfiles.length) {
    html += '<div class="section-title">📁 Profiles</div><table><thead><tr>'+
      '<th>名称</th><th>模式</th><th>状态</th><th>操作</th></tr></thead><tbody>'+
      userProfiles.map(p =>
        '<tr><td>'+esc(p.name)+'</td><td>'+esc(p.mode)+'</td>'+
        '<td>'+(p.active ? '<span style="color:var(--green)">● 生效中</span>' : '')+'</td>'+
        '<td>'+(!p.active ? '<button class="btn" data-activate="'+esc(p.name)+'">激活</button>' : '')+
        '<button class="btn" data-diff="'+esc(p.name)+'">对比</button></td></tr>'
      ).join('')+'</tbody></table><div id="diffView"></div>';
  }
  // R14 白名单字段编辑
  html += '<div class="section-title">✏️ 编辑配置（白名单字段，写入当前生效配置文件）</div>'+
    '<div class="run-form"><select id="editField" class="run-input">'+
    ['worker_models','worker_backends','local_models','local_model_cost','goal','evaluator',
     'plan_api.worker_base_url','planner_api.base_url'].map(f => '<option value="'+f+'">'+f+'</option>').join('')+
    '</select>'+
    '<input id="editValue" class="run-input" style="flex:3" placeholder="JSON 值，如 {&quot;easy&quot;:&quot;claude-haiku-4-5&quot;} 或 [&quot;m1&quot;]">'+
    '<button class="btn" id="btnEditSave">💾 保存</button>'+
    '<span id="editMsg" style="font-size:12px"></span></div>';
  // 只读配置展示（原有）
  html += '<div class="section-title">⚙️ 用户配置（生效值，api_key 已脱敏）</div>';
  html += '<div class="json-view">'+esc(JSON.stringify(d.config, null, 2))+'</div>';
  html += '<div class="kv" style="margin-top:10px"><dt>配置路径</dt><dd>'+esc(d.config_path)+'</dd></div>';
  if (d.role_skill_map) {
    html += '<div class="section-title">🎭 角色-Skill 映射</div>';
    html += '<div class="json-view">'+esc(JSON.stringify(d.role_skill_map, null, 2))+'</div>';
    html += '<div class="kv" style="margin-top:10px"><dt>路径</dt><dd>'+esc(d.role_skill_map_path)+'</dd></div>';
  }
  document.getElementById('mainView').innerHTML = html;
  // 绑定操作
  const bindOp = (id, fn, confirmMsg) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.onclick = async () => {
      if (confirmMsg && !confirm(confirmMsg)) return;
      el.disabled = true;
      try { await fn(); } catch (e) { alert('❌ 操作失败: '+e.message); }
      loadConfig();
    };
  };
  bindOp('btnLocal', () => postJSON('/api/profile/local', {}),
    '切换到纯本地模式？\\n将探测 localhost:4000 代理并生成 local profile（当前配置自动备份）。');
  bindOp('btnLocalFix', () => postJSON('/api/profile/local', {}),
    '重新生成 local profile？（当前配置自动备份）');
  bindOp('btnCloud', () => postJSON('/api/profile/cloud', {}),
    '恢复云端配置？（当前配置自动备份）');
  document.querySelectorAll('[data-activate]').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.activate;
      if (!confirm('激活 profile「'+name+'」？（当前配置自动备份）')) return;
      btn.disabled = true;
      try { await postJSON('/api/profile/activate', {name}); }
      catch (e) { alert('❌ 操作失败: '+e.message); }
      loadConfig();
    };
  });
  // R15 diff 对比
  document.querySelectorAll('[data-diff]').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.diff;
      const slot = document.getElementById('diffView');
      slot.innerHTML = '<div class="loading">对比中…</div>';
      try {
        const d = await api('/api/config/diff?name='+encodeURIComponent(name));
        if (!d.diff_count) {
          slot.innerHTML = '<div style="color:var(--green);margin:8px 0">✅ 当前配置与「'+esc(name)+'」无差异</div>';
          return;
        }
        const rows = d.diffs.map(x =>
          '<tr><td style="font-family:Menlo,monospace;font-size:12px">'+esc(x.field)+'</td>'+
          '<td class="json-view" style="max-height:120px">'+esc(JSON.stringify(x.current))+'</td>'+
          '<td class="json-view" style="max-height:120px">'+esc(JSON.stringify(x.target))+'</td></tr>').join('');
        slot.innerHTML = '<div class="section-title">当前生效 vs「'+esc(name)+'」（'+d.diff_count+' 处差异）</div>'+
          '<table><thead><tr><th>字段</th><th>当前</th><th>'+esc(name)+'</th></tr></thead><tbody>'+rows+'</tbody></table>';
      } catch (e) {
        slot.innerHTML = '<div class="err">'+esc(e.message)+'</div>';
      }
    };
  });
  // R14 编辑保存
  const btnSave = document.getElementById('btnEditSave');
  if (btnSave) btnSave.onclick = async () => {
    const field = document.getElementById('editField').value;
    const raw = document.getElementById('editValue').value.trim();
    const msg = document.getElementById('editMsg');
    let value;
    try { value = JSON.parse(raw); }
    catch (e) { msg.textContent = '⚠️ 值必须是合法 JSON'; msg.style.color = 'var(--yellow)'; return; }
    if (!confirm('保存字段「'+field+'」到当前生效配置？\\n新任务立即生效。')) return;
    btnSave.disabled = true;
    try {
      const d = await putJSON('/api/config', {field, value});
      msg.textContent = '✅ 已保存到 '+d.saved_to+'（'+d.effective+'）';
      msg.style.color = 'var(--green)';
      setTimeout(loadConfig, 1000);
    } catch (e) {
      msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)';
      btnSave.disabled = false;
    }
  };
}

async function putJSON(path, body) {
  const headers = {'Content-Type': 'application/json'};
  if (authToken) headers['Authorization'] = 'Bearer '+authToken;
  const r = await fetch(path, {method: 'PUT', headers, body: JSON.stringify(body || {})});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('HTTP '+r.status));
  return data;
}

async function loadStorage() {
  const d = await api('/api/storage');
  let html = '<div class="section-title">💾 磁盘占用</div>';
  if (d.alert) html += '<div class="warn-banner">⚠️ '+esc(d.alert)+'</div>';
  html += '<div class="kpi-grid">'+
    kpiCard('总占用', d.total_size_mb+' MB', 'blue')+
    kpiCard('任务目录', d.task_count, '')+
    kpiCard('孤儿目录', d.orphan_count, d.orphan_count>0?'yellow':'')+
    '</div>';
  if (d.orphan_count > 0) {
    html += '<div class="warn-banner">⚠️ 检测到 '+d.orphan_count+' 个孤儿目录（无 meta.json，可能是异常中断的残留）。'+
            '可用 <code>agent_go clean --orphans</code> 清理。</div>';
  }
  html += '<div class="section-title">📦 最大任务目录 Top 20</div>';
  const rows = (d.top_tasks||[]).map((t,i) =>
    '<tr><td>'+(i+1)+'</td><td>'+esc(t.name)+'</td>'+
    '<td>'+(t.size/1024/1024).toFixed(2)+' MB</td>'+
    '<td>'+(t.has_meta?'✓':'<span class="st-failed">✗ 孤儿</span>')+'</td></tr>'
  ).join('');
  html += '<table><thead><tr><th>#</th><th>任务</th><th>大小</th><th>meta</th></tr></thead><tbody>'+rows+'</tbody></table>';
  // U6：写操作审计（R16 消费端闭环）
  try {
    const audit = await api('/api/audit');
    if (audit.records.length) {
      const aRows = audit.records.map(r =>
        '<tr><td style="white-space:nowrap">'+esc((r.ts||'').replace('T',' ').slice(0,19))+'</td>'+
        '<td>'+esc(r.op||'')+'</td>'+
        '<td>'+(r.ok ? '<span style="color:var(--green)">✓</span>' : '<span style="color:var(--red)">✗</span>')+'</td>'+
        '<td style="font-size:11px;color:var(--dim);max-width:340px;word-break:break-all">'+
          esc(JSON.stringify(r.params||{}).slice(0,120))+'</td>'+
        '<td style="font-size:11px">'+esc(r.auth||'-')+'</td></tr>').join('');
      html += '<div class="section-title">📜 操作审计（最近 '+audit.records.length+' / 共 '+audit.total+' 条）</div>'+
        '<table><thead><tr><th>时间</th><th>操作</th><th>结果</th><th>参数摘要</th><th>操作者</th></tr></thead>'+
        '<tbody>'+aRows+'</tbody></table>';
    }
  } catch (e) {}
  document.getElementById('mainView').innerHTML = html;
}

async function loadInsight() {
  const main = document.getElementById('mainView');
  main.innerHTML = '<div class="loading">加载中…</div>';
  const [insights, decisions, batches] = await Promise.all([
    api('/api/insights'), api('/api/decisions'), api('/api/bench-batches'),
  ]);
  let html = '<div class="section-title">🧠 决策洞察</div>';
  // 生成表单
  const batchOpts = (batches.batches || []).map(b => {
    const name = (typeof b === 'string') ? b : (b.name || '');
    const label = (typeof b === 'string') ? b : (b.name + '（' + (b.records||0) + ' 条）');
    return '<option value="'+esc(name)+'">'+esc(label)+'</option>';
  }).join('');
  html += '<div class="run-form">'+
    '<select id="insBatch" class="run-input" style="flex:2">'+batchOpts+'</select>'+
    '<input id="insGoal" class="run-input" style="flex:2" placeholder="分析目标，如：hard 通过率保持 100% 且成本降低">'+
    '<input id="insPlan" class="run-input" style="flex:2" placeholder="预设计划（可选）：换模型/降级链/…">'+
    '<button class="btn primary" id="btnInsightGen">🤖 生成洞察</button>'+
    '<span id="insMsg" style="margin-left:8px;font-size:12px"></span></div>';
  // 报告列表
  html += '<div class="section-title">📄 洞察报告（'+(insights.reports||[]).length+'）</div>';
  if ((insights.reports||[]).length) {
    html += '<table><thead><tr><th>报告</th><th>时间</th><th>大小</th><th>操作</th></tr></thead><tbody>'+
      (insights.reports||[]).map(r =>
        '<tr><td>'+esc(r.name)+'</td>'+
        '<td>'+esc(new Date(r.mtime*1000).toLocaleString('zh-CN'))+'</td>'+
        '<td>'+(r.size/1024).toFixed(1)+' KB</td>'+
        '<td><button class="btn" data-insview="'+esc(r.name)+'">查看</button></td></tr>'
      ).join('')+'</tbody></table>';
  } else {
    html += '<div class="loading">暂无报告（选择批次点「生成洞察」创建）</div>';
  }
  html += '<div id="insReportView"></div>';
  // 决策历史
  html += '<div class="section-title">📜 决策历史（'+(decisions.total||0)+'）</div>';
  if ((decisions.records||[]).length) {
    html += '<table><thead><tr><th>时间</th><th>变更</th><th>目标/理由</th><th>来源</th><th>确认人</th></tr></thead><tbody>'+
      (decisions.records||[]).map(d =>
        '<tr><td style="white-space:nowrap">'+esc((d.ts||'').replace('T',' ').slice(0,19))+'</td>'+
        '<td style="font-size:12px">'+esc(d.change||'')+'</td>'+
        '<td style="font-size:12px;color:var(--dim)">'+esc((d.goal||'')+(d.expected_impact ? ' → '+d.expected_impact : '')).slice(0,80)+'</td>'+
        '<td>'+esc(d.source||'-')+'</td><td>'+esc(d.confirmer||'-')+'</td></tr>'
      ).join('')+'</tbody></table>';
  } else {
    html += '<div class="loading">暂无决策记录（模型切换/配置修改/recommend 应用将自动记录）</div>';
  }
  main.innerHTML = html;
  // 绑定：生成
  const btnGen = document.getElementById('btnInsightGen');
  if (btnGen) btnGen.onclick = async () => {
    const batch = document.getElementById('insBatch').value;
    const goal = document.getElementById('insGoal').value.trim();
    const plan = document.getElementById('insPlan').value.trim();
    const msg = document.getElementById('insMsg');
    btnGen.disabled = true;
    msg.textContent = '生成中（证据物化 + LLM 推理约 1-2 分钟）…'; msg.style.color = 'var(--dim)';
    try {
      const d = await postJSON('/api/insight/generate', {batch, goal, plan});
      msg.textContent = '✅ 已生成: '+(d.report_name || ''); msg.style.color = 'var(--green)';
      setTimeout(loadInsight, 1000);
    } catch (e) {
      msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)';
      btnGen.disabled = false;
    }
  };
  // 绑定：查看
  document.querySelectorAll('[data-insview]').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.insview;
      const slot = document.getElementById('insReportView');
      slot.innerHTML = '<div class="loading">加载报告…</div>';
      try {
        const d = await api('/api/insights/'+encodeURIComponent(name));
        slot.innerHTML = '<div class="section-title">📄 '+esc(name)+'</div>'+
          '<div class="json-view" style="max-height:520px">'+esc(d.content)+'</div>';
      } catch (e) {
        slot.innerHTML = '<div class="err">'+esc(e.message)+'</div>';
      }
    };
  });
}

function setConn(ok) {
  const b = document.getElementById('connBadge');
  b.textContent = ok ? '● 已连接' : '○ 连接断开';
  b.style.color = ok ? 'var(--green)' : 'var(--red)';
}

function connectSSE() {
  if (sse) sse.close();
  const qs = '?interval=5' + (authToken ? '&token='+encodeURIComponent(authToken) : '');
  sse = new EventSource('/api/events'+qs);
  sse.addEventListener('message', e => {
    try {
      const m = JSON.parse(e.data);
      if (m.type === 'refresh') {
        // 任务/看板视图自动刷新（其他视图按需手动刷新，避免覆盖用户正在看的页面）
        if (currentView === 'tasks') loadTasks();
        if (currentView === 'kanban') loadKanban();
      }
    } catch(_) {}
  });
  // EventSource 自带自动重连；仅在彻底关闭（CLOSED）时才手动重建，避免连接叠加
  sse.onerror = () => {
    if (sse.readyState === EventSource.CLOSED) setTimeout(connectSSE, 5000);
  };
}

document.getElementById('refreshBtn').onclick = () => switchView(currentView);
document.getElementById('searchInput').addEventListener('input', renderTasks);
document.querySelectorAll('.nav-tab').forEach(tab => {
  tab.addEventListener('click', () => switchView(tab.dataset.view));
});
bindKanbanKeyboard();
loadTasks();
connectSSE();
</script>
</body>
</html>
"""
