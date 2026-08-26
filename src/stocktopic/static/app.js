const state = { data: null };
const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
}[c]));

document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.tab,.tab-panel').forEach(node => node.classList.remove('active'));
  button.classList.add('active');
  $(`#${button.dataset.tab}`).classList.add('active');
}));

$('#refresh').addEventListener('click', load);
$('#wecomTest').addEventListener('click', async () => {
  try {
    await api('/api/v1/admin/wecom-test', { method: 'POST' });
    toast('企业微信测试消息已发送');
  } catch (error) { toast(error.message); }
});

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-StockTopic-Request': '1',
      ...(options.headers || {})
    }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败 ${response.status}`);
  return body;
}

async function load() {
  try {
    state.data = await api('/api/v1/dashboard');
    render();
  } catch (error) { toast(error.message); }
}

function render() {
  const { health, themes, anomalies, alerts } = state.data;
  const pending = themes.filter(x => x.status === 'pending');
  const confirmed = themes.filter(x => x.status === 'confirmed');
  const market = health.market;
  $('#marketState').textContent = market.realtime_collection_enabled ? '盘中采集中' : `${market.session} · 待机`;
  $('#marketState').classList.toggle('live', market.realtime_collection_enabled);
  $('#universeCount').textContent = health.universe_count?.toLocaleString() || '—';
  $('#lastRun').textContent = health.latest_quote_run?.started_at?.slice(11, 16) || '—';
  $('#lastRunDetail').textContent = health.latest_quote_run?.status || '等待行情';
  $('#candidateCount').textContent = pending.length;
  $('#confirmedCount').textContent = confirmed.length;
  $('#anomalyCount').textContent = anomalies.length;
  $('#alertCount').textContent = alerts.length;
  renderThemes('#candidateList', pending, true);
  renderThemes('#confirmedList', confirmed, false);
  renderAnomalies(anomalies);
  renderAlerts(alerts);
}

function renderThemes(selector, items, pending) {
  const root = $(selector);
  root.classList.toggle('empty', !items.length);
  root.innerHTML = items.length
    ? items.map(theme => themeCard(theme, pending)).join('')
    : (pending ? '暂无候选题材' : '暂无已确认题材');
  root.querySelectorAll('[data-action]').forEach(button =>
    button.addEventListener('click', () => themeAction(button))
  );
}

function themeCard(theme, pending) {
  const title = theme.final_name || theme.suggested_name || theme.provisional_name;
  const members = theme.members.filter(x => x.active).map(member => {
    const reasons = (member.evidence.anomaly_reasons || []).join('；');
    return `<div class="member"><strong>${escapeHtml(member.name)} <small>${escapeHtml(member.code)}</small></strong><small title="${escapeHtml(reasons)}">${escapeHtml(reasons)}</small></div>`;
  }).join('');
  const score = theme.score;
  const scoreHtml = score ? `<div class="scores">
    <div class="score heat"><span>HEAT</span><strong>${score.heat}</strong></div>
    <div class="score persistence"><span>PERSISTENCE</span><strong>${score.persistence}</strong></div>
    <div class="score risk"><span>ENTRY RISK</span><strong>${score.entry_risk}</strong></div>
  </div><p style="margin-top:10px">${escapeHtml(score.lifecycle)} · Day ${score.details.day_number}${score.leader_theme_divergence ? ' · 龙头—板块背离' : ''}</p>`
    : '<p style="margin-top:13px">等待人工确认，评分已锁定。</p>';
  const actions = pending ? `<div class="actions">
    <button class="action primary" data-action="confirm" data-id="${theme.id}" data-name="${escapeHtml(title)}">确认题材</button>
    <button class="action" data-action="explain" data-id="${theme.id}">重新解释</button>
    <button class="action" data-action="merge" data-id="${theme.id}">合并</button>
    <button class="action" data-action="split" data-id="${theme.id}">拆分</button>
    <button class="action danger" data-action="reject" data-id="${theme.id}">忽略</button>
  </div>` : `<div class="actions"><button class="action" data-action="explain" data-id="${theme.id}">更新新闻解释</button></div>`;
  return `<article class="theme-card"><div class="theme-head"><div><div class="theme-title">${escapeHtml(title)}</div><p>${escapeHtml(theme.discovery_reason)}</p></div><span class="badge positive">${escapeHtml(theme.shared_tag)}</span></div>${scoreHtml}<div class="member-grid">${members}</div>${actions}</article>`;
}

async function themeAction(button) {
  const id = Number(button.dataset.id);
  try {
    if (button.dataset.action === 'confirm') {
      const finalName = prompt('确认题材名称', button.dataset.name);
      if (!finalName) return;
      await api(`/api/v1/themes/${id}/confirm`, {
        method: 'POST', body: JSON.stringify({final_name: finalName})
      });
    } else if (button.dataset.action === 'reject') {
      if (!confirm('确定忽略这个候选题材？')) return;
      await api(`/api/v1/themes/${id}/reject`, {method: 'POST'});
    } else if (button.dataset.action === 'explain') {
      toast('正在搜索最近72小时催化…');
      await api(`/api/v1/themes/${id}/explain`, {method: 'POST'});
    } else if (button.dataset.action === 'merge') {
      const raw = prompt('输入要合并进当前题材的候选ID，多个用逗号分隔');
      if (!raw) return;
      const source_ids = raw.split(',').map(Number).filter(Boolean);
      await api(`/api/v1/themes/${id}/merge`, {method: 'POST', body: JSON.stringify({source_ids})});
    } else if (button.dataset.action === 'split') {
      const codes = prompt('输入要拆出的股票代码，多个用逗号分隔');
      if (!codes) return;
      const new_name = prompt('新题材名称');
      if (!new_name) return;
      await api(`/api/v1/themes/${id}/split`, {
        method: 'POST',
        body: JSON.stringify({member_codes: codes.split(',').map(x => x.trim()), new_name})
      });
    }
    await load();
    toast('操作完成');
  } catch (error) { toast(error.message); }
}

function renderAnomalies(items) {
  const root = $('#anomalyList');
  root.classList.toggle('empty', !items.length);
  root.innerHTML = items.length ? items.map(item => `<article class="event"><time>${escapeHtml(item.captured_at.slice(11, 16))}</time><div><strong>${escapeHtml(item.name)} · ${escapeHtml(item.code)}</strong><small>${escapeHtml(item.reasons.join('；'))}</small></div><div class="pct ${item.direction === 'negative' ? 'negative' : ''}">${item.pct_change > 0 ? '+' : ''}${item.pct_change.toFixed(2)}%</div></article>`).join('') : '当前没有异动';
}

function renderAlerts(items) {
  const root = $('#alertList');
  root.classList.toggle('empty', !items.length);
  root.innerHTML = items.length ? items.map(item => `<article class="event"><time>${escapeHtml(item.created_at.slice(11, 16))}</time><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.body)}</small></div><span class="badge ${item.severity === 'critical' ? 'negative' : 'positive'}">${escapeHtml(item.severity)}</span></article>`).join('') : '暂无预警';
}

let toastTimer;
function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 2600);
}

load();
setInterval(load, 60_000);
