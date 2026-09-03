const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
}[character]));

let searchTimer = null;
let lastData = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    credentials: 'same-origin',
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-StockTopic-Request': '1',
      ...(options.headers || {})
    }
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    location.href = '/';
    throw new Error('登录已失效，请先返回主页面登录');
  }
  if (!response.ok) throw new Error(body.detail || `请求失败 ${response.status}`);
  return body;
}

function currentQuery() {
  const params = new URLSearchParams();
  const source = $('#sourceFilter').value;
  const query = $('#searchInput').value.trim();
  const minMembers = $('#minMembers').value;
  if (source !== 'all') params.set('source', source);
  if (query) params.set('q', query);
  params.set('min_members', minMembers);
  return params.toString();
}

async function loadGraph(showToast = false) {
  $('#statusLine').textContent = '正在读取图谱…';
  try {
    const data = await api(`/api/v1/theme-graph?${currentQuery()}`);
    lastData = data;
    render(data);
    if (showToast) toast('图谱已刷新');
  } catch (error) {
    $('#statusLine').textContent = error.message;
    $('#graphList').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

function render(data) {
  const stats = data.stats || {};
  $('#tradeDate').textContent = formatDate(data.trade_date);
  $('#nodeCount').textContent = Number(stats.nodes || 0).toLocaleString();
  $('#memberCount').textContent = Number(stats.members || 0).toLocaleString();
  $('#crossCount').textContent = Number(stats.cross_source_nodes || 0).toLocaleString();
  $('#sourceStats').innerHTML = (data.sources || []).map(source => `
    <article class="source-card">
      <div><b>${escapeHtml(source.label)}</b><small>${Number(source.nodes || 0).toLocaleString()} 个节点</small></div>
      <small>${Number(source.members || 0).toLocaleString()} 股 · ${Number(source.edges || 0).toLocaleString()} 条边</small>
    </article>
  `).join('');
  const items = data.items || [];
  $('#statusLine').textContent = data.trade_date
    ? `${formatDate(data.trade_date)} · 当前显示 ${items.length} 个题材节点${data.synced_at ? ` · 同步 ${formatTime(data.synced_at)}` : ''}`
    : '暂无题材图谱数据，请点击“同步图谱”。';
  $('#graphList').innerHTML = items.length
    ? items.map(renderNode).join('')
    : '<div class="empty">没有符合当前筛选条件的题材节点。</div>';
}

function renderNode(node) {
  const sourceBadges = (node.sources || []).map(source =>
    `<span class="badge">${escapeHtml(source.label)}</span>`
  ).join('');
  const cross = node.cross_source ? '<span class="badge cross">多源共识</span>' : '';
  const hot = node.hot_rank ? ` · 热度#${node.hot_rank}` : '';
  return `
    <details data-node-id="${escapeHtml(node.id)}">
      <summary>
        <div class="node-title">
          <strong>${escapeHtml(node.name)}</strong>
          <small>${Number(node.member_count || 0)} 只成分股${hot}</small>
        </div>
        <div class="badges">${cross}${sourceBadges}</div>
        <span class="chevron">›</span>
      </summary>
      <div class="node-body">
        <div class="member-grid">
          ${(node.members || []).map(renderMember).join('')}
        </div>
      </div>
    </details>
  `;
}

function renderMember(member) {
  const sources = (member.sources || []).map(source =>
    `<span class="badge">${escapeHtml(source.label)}</span>`
  ).join('');
  const meta = [member.market, member.industry].filter(Boolean).map(escapeHtml).join(' · ');
  const reasons = (member.reasons || []).map(reason => `
    <li><b>${escapeHtml(reason.source_label)}</b> ${escapeHtml(reason.text)}</li>
  `).join('');
  return `
    <article class="member-card">
      <div class="member-head">
        <div><strong>${escapeHtml(member.name)}</strong><code>${escapeHtml(member.code)}</code></div>
        ${member.source_count > 1 ? `<span class="badge cross">${member.source_count}源</span>` : ''}
      </div>
      ${meta ? `<div class="member-meta">${meta}</div>` : ''}
      <div class="member-sources">${sources}</div>
      ${reasons ? `<ul class="reason-list">${reasons}</ul>` : ''}
    </article>
  `;
}

async function refreshGraph() {
  const button = $('#refreshGraph');
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '同步中…';
  try {
    const result = await api('/api/v1/admin/refresh-theme-graph', { method: 'POST', body: '{}' });
    toast(`已同步 ${result.trade_date} · ${Number(result.rows || 0).toLocaleString()} 条关系`);
    await loadGraph();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadGraph(), 220);
}

$('#searchInput').addEventListener('input', scheduleSearch);
$('#sourceFilter').addEventListener('change', () => loadGraph());
$('#minMembers').addEventListener('change', () => loadGraph());
$('#refreshGraph').addEventListener('click', refreshGraph);
$('#expandAll').addEventListener('click', () => $$('details').forEach(item => { item.open = true; }));
$('#collapseAll').addEventListener('click', () => $$('details').forEach(item => { item.open = false; }));

function formatDate(value) {
  const text = String(value || '');
  return /^\d{8}$/.test(text) ? `${text.slice(0,4)}-${text.slice(4,6)}-${text.slice(6,8)}` : (text || '—');
}

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

let toastTimer = null;
function toast(message, error = false) {
  const node = $('#toast');
  node.textContent = message;
  node.style.background = error ? 'rgba(160,20,20,.94)' : 'rgba(30,30,30,.9)';
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 2600);
}

loadGraph();
