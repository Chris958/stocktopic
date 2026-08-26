const state = {
  data: null,
  auth: sessionStorage.getItem('stocktopicAuth') || '',
  view: 'candidates',
  anomalyFilter: 'all',
  sheet: null,
  lastFocus: null
};

const views = {
  candidates: {
    kicker: '候选题材',
    title: '机器发现，人工确认',
    description: '股票成员由确定性规则产生，AI只负责命名与解释。'
  },
  confirmed: {
    kicker: '正式题材',
    title: '题材生命周期',
    description: '热度、持续性和接盘风险独立呈现，避免用单一总分掩盖风险。'
  },
  anomalies: {
    kicker: '全市场异动池',
    title: '资金行为正在发生',
    description: '硬事件直接进入；普通股票至少满足两项异动条件。'
  },
  alerts: {
    kicker: '系统预警',
    title: '只打扰真正重要的时刻',
    description: '聚焦高价值机会、接盘风险、龙头—板块背离和数据故障。'
  }
};

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
}[character]));

function basicAuthorization(username, password) {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  let binary = '';
  bytes.forEach(byte => { binary += String.fromCharCode(byte); });
  return `Basic ${btoa(binary)}`;
}

async function api(path, options = {}, authOverride = null) {
  const authorization = authOverride ?? state.auth;
  const response = await fetch(path, {
    cache: 'no-store',
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-StockTopic-Request': '1',
      ...(authorization ? { Authorization: authorization } : {}),
      ...(options.headers || {})
    }
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    if (!authOverride) showLogin('登录已失效，请重新输入密码。');
    throw new Error('用户名或密码不正确');
  }
  if (!response.ok) throw new Error(body.detail || `请求失败 ${response.status}`);
  return body;
}

function showLogin(message = '') {
  state.auth = '';
  sessionStorage.removeItem('stocktopicAuth');
  $('#loginBackdrop').classList.remove('authenticated');
  $('#loginError').textContent = message;
  requestAnimationFrame(() => $('#loginPassword').focus());
}

function hideLogin() {
  $('#loginBackdrop').classList.add('authenticated');
  $('#loginError').textContent = '';
  $('#loginPassword').value = '';
}

$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#loginSubmit');
  const username = $('#loginUsername').value.trim();
  const password = $('#loginPassword').value;
  const authorization = basicAuthorization(username, password);
  button.disabled = true;
  button.textContent = '正在验证…';
  $('#loginError').textContent = '';
  try {
    state.data = await api('/api/v1/dashboard', {}, authorization);
    state.auth = authorization;
    sessionStorage.setItem('stocktopicAuth', authorization);
    hideLogin();
    render();
    toast('连接成功');
  } catch (error) {
    $('#loginError').textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = '安全登录';
  }
});

$$('[data-view]').forEach(button => button.addEventListener('click', () => {
  setView(button.dataset.view);
}));

function setView(view) {
  if (!views[view]) return;
  state.view = view;
  $$('.nav-item,.mobile-tab').forEach(button => {
    const selected = button.dataset.view === view;
    button.classList.toggle('active', selected);
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $$('.view-panel').forEach(panel => panel.classList.toggle('active', panel.id === view));
  const content = views[view];
  $('#sectionKicker').textContent = content.kicker;
  $('#sectionTitle').textContent = content.title;
  $('#sectionDescription').textContent = content.description;
  updateSectionCount();
  if (window.innerWidth < 861) {
    $('.content-shell').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

$$('[data-filter]').forEach(button => button.addEventListener('click', () => {
  state.anomalyFilter = button.dataset.filter;
  $$('[data-filter]').forEach(item => item.classList.toggle('active', item === button));
  renderAnomalies(state.data?.anomalies || []);
  updateSectionCount();
}));

$('#refresh').addEventListener('click', () => load(true));
$('#accountButton').addEventListener('click', event => openSheet('logout', {}, event.currentTarget));
$('#wecomTest').addEventListener('click', event => openSheet('wecom', {}, event.currentTarget));
$('#sheetBackdrop').addEventListener('click', closeSheet);
$$('[data-close-sheet]').forEach(button => button.addEventListener('click', closeSheet));
document.addEventListener('keydown', event => {
  const sheet = $('#actionSheet');
  if (event.key === 'Escape' && !sheet.hidden) closeSheet();
  if (event.key !== 'Tab' || sheet.hidden) return;
  const focusable = [...sheet.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled])')];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

async function load(manual = false) {
  if (!state.auth) return showLogin();
  const refresh = $('#refresh');
  refresh.classList.add('loading');
  try {
    state.data = await api('/api/v1/dashboard');
    render();
    if (manual) toast('数据已刷新');
  } catch (error) {
    if (state.auth) toast(error.message, true);
  } finally {
    refresh.classList.remove('loading');
  }
}

function render() {
  if (!state.data) return;
  const { health, themes, anomalies, alerts, data_context: dataContext = {} } = state.data;
  const pending = themes.filter(theme => theme.status === 'pending');
  const confirmed = themes.filter(theme => theme.status === 'confirmed');
  const market = health.market;
  const marketNode = $('#marketState');
  marketNode.classList.remove('loading', 'live', 'warning');
  marketNode.classList.add(market.realtime_collection_enabled ? 'live' : 'warning');
  marketNode.querySelector('strong').textContent = market.realtime_collection_enabled
    ? '盘中采集中'
    : marketLabel(market.session);
  marketNode.querySelector('small').textContent = market.realtime_collection_enabled
    ? '5分钟实时行情'
    : dataContext.has_intraday_data
      ? `保留最近交易日 · ${formatDate(dataContext.anomaly_trade_date)}`
      : market.reason === 'closed'
        ? '等待下个交易窗口首次采集'
        : marketReason(market.reason);
  $('#universeCount').textContent = health.universe_count?.toLocaleString() || '—';
  $('#candidateMetric').textContent = pending.length.toLocaleString();
  $('#confirmedMetric').textContent = confirmed.length.toLocaleString();
  $('#lastRun').textContent = formatTime(health.latest_quote_run?.started_at);
  $('#lastRunDetail').textContent = runStatus(health.latest_quote_run?.status);
  $('#candidateCount').textContent = pending.length;
  $('#confirmedCount').textContent = confirmed.length;
  $('#anomalyCount').textContent = anomalies.length;
  $('#alertCount').textContent = alerts.length;
  renderThemes('#candidateList', pending, true);
  renderThemes('#confirmedList', confirmed, false);
  renderAnomalies(anomalies);
  renderAlerts(alerts);
  updateSectionCount();
}

function marketLabel(session) {
  return ({
    opening_auction: '集合竞价', morning: '上午交易', afternoon: '下午交易',
    lunch_break: '午间休市', auction_gap: '竞价间隙', pre_market: '盘前待机', closed: '已收市'
  })[session] || '系统待机';
}

function marketReason(reason) {
  return ({
    exchange_closed: '交易所休市 · 不采集',
    calendar_unknown_fail_closed: '日历未知 · 已停止采集',
    lunch_break: '午休期间不采集',
    auction_gap: '09:25–09:30不采集',
    pre_market: '等待交易窗口',
    closed: '非交易时段不采集'
  })[reason] || '实时采集已停止';
}

function runStatus(status) {
  return ({ success: '采集正常', failed: '采集失败', degraded: '降级运行' })[status] || '等待行情';
}

function formatTime(value) {
  if (!value) return '—';
  const match = String(value).match(/T?(\d{2}):(\d{2})/);
  return match ? `${match[1]}:${match[2]}` : '—';
}

function formatDate(value) {
  const match = String(value || '').match(/(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[2]}-${match[3]}` : '—';
}

function renderThemes(selector, items, pending) {
  const root = $(selector);
  root.classList.remove('skeleton-list');
  root.innerHTML = items.length
    ? items.map(theme => themeCard(theme, pending)).join('')
    : emptyState(pending ? '暂无候选题材' : '暂无正式题材', pending
      ? '机器发现共性异动后会立即显示在这里。'
      : '确认候选题材后，三项评分会在这里出现。');
  root.querySelectorAll('[data-action]').forEach(button => {
    button.addEventListener('click', event => {
      const theme = items.find(item => Number(item.id) === Number(button.dataset.id));
      openSheet(button.dataset.action, theme || {}, event.currentTarget);
    });
  });
}

function themeCard(theme, pending) {
  const title = theme.final_name || theme.suggested_name || theme.provisional_name;
  const activeMembers = theme.members.filter(member => member.active);
  const members = activeMembers.map(member => {
    const reasons = (member.evidence?.anomaly_reasons || []).join('；') || '共享题材标签';
    return `<div class="member" title="${escapeHtml(reasons)}"><strong>${escapeHtml(member.name)}</strong><small>${escapeHtml(member.code)}</small></div>`;
  }).join('');
  const score = theme.score;
  const scoreHtml = score ? `<div class="score-grid">
    ${scoreCell('热度', score.heat, 'heat')}
    ${scoreCell('持续性', score.persistence, 'persistence')}
    ${scoreCell('接盘风险', score.entry_risk, 'risk')}
  </div>
  <div class="lifecycle-row">
    <span class="lifecycle-chip">${escapeHtml(score.lifecycle)} · Day ${escapeHtml(score.details.day_number)}</span>
    ${score.leader_theme_divergence ? '<span class="risk-note">龙头—板块背离</span>' : ''}
  </div>` : `<div class="locked-score"><span class="lock-icon">◇</span><span>确认前评分锁定，只展示股票与发现原因</span></div>`;
  const actions = pending ? `<div class="card-actions">
    <button class="primary-button pressable" data-action="confirm" data-id="${theme.id}">确认题材</button>
    <button class="secondary-button pressable" data-action="explain" data-id="${theme.id}">AI解释</button>
    <button class="text-button pressable" data-action="merge" data-id="${theme.id}">合并</button>
    <button class="text-button pressable" data-action="split" data-id="${theme.id}">拆分</button>
    <button class="text-button danger pressable" data-action="reject" data-id="${theme.id}">忽略</button>
  </div>` : `<div class="card-actions">
    <button class="secondary-button pressable" data-action="explain" data-id="${theme.id}">更新新闻解释</button>
  </div>`;
  return `<article class="theme-card">
    <div class="theme-head"><div><div class="theme-title">${escapeHtml(title)}</div><p>${escapeHtml(theme.discovery_reason)}</p></div><span class="theme-tag">${escapeHtml(theme.shared_tag)}</span></div>
    ${scoreHtml}
    <div class="members" aria-label="题材成员">${members}</div>
    <p class="theme-footnote">${activeMembers.length}只成员 · Day 1 ${escapeHtml(theme.day1_date)}</p>
    ${actions}
  </article>`;
}

function scoreCell(label, value, className) {
  const score = Math.max(0, Math.min(100, Number(value) || 0));
  return `<div class="score-cell ${className}"><span>${label}</span><strong>${escapeHtml(value)}</strong><div class="score-track"><i style="width:${score}%"></i></div></div>`;
}

function renderAnomalies(items) {
  const filtered = state.anomalyFilter === 'all'
    ? items
    : items.filter(item => item.direction === state.anomalyFilter);
  $('#anomalyList').innerHTML = filtered.length ? filtered.map(item => {
    const negative = item.direction === 'negative';
    const pct = Number(item.pct_change || 0);
    return `<article class="event-card">
      <time class="event-time">${escapeHtml(formatTime(item.captured_at))}</time>
      <div class="event-main"><strong>${escapeHtml(item.name)} · ${escapeHtml(item.code)}</strong><small>${escapeHtml((item.reasons || []).join('；'))}</small></div>
      <div class="event-value ${negative ? 'negative' : ''}">${pct > 0 ? '+' : ''}${pct.toFixed(2)}%</div>
    </article>`;
  }).join('') : emptyState('当前没有匹配异动', '系统仅显示符合平衡模式规则的股票。');
}

function renderAlerts(items) {
  $('#alertList').innerHTML = items.length ? items.map(item => `<article class="event-card">
    <time class="event-time">${escapeHtml(formatTime(item.created_at))}</time>
    <div class="event-main"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.body)}</small></div>
    <span class="event-badge ${item.severity === 'critical' ? 'critical' : 'high'}">${escapeHtml(item.severity)}</span>
  </article>`).join('') : emptyState('暂无预警', '没有高风险事件，也没有需要处理的数据故障。');
}

function emptyState(title, detail) {
  return `<div class="empty-state"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

function updateSectionCount() {
  if (!state.data) return;
  const { themes, anomalies, alerts } = state.data;
  const counts = {
    candidates: themes.filter(theme => theme.status === 'pending').length,
    confirmed: themes.filter(theme => theme.status === 'confirmed').length,
    anomalies: state.anomalyFilter === 'all'
      ? anomalies.length
      : anomalies.filter(item => item.direction === state.anomalyFilter).length,
    alerts: alerts.length
  };
  $('#sectionCount').textContent = counts[state.view].toLocaleString();
}

function openSheet(action, theme, trigger) {
  state.sheet = { action, theme };
  state.lastFocus = trigger || document.activeElement;
  const title = theme.final_name || theme.suggested_name || theme.provisional_name || '';
  const content = sheetContent(action, title);
  $('#sheetKicker').textContent = content.kicker;
  $('#sheetTitle').textContent = content.title;
  $('#sheetFields').innerHTML = content.fields;
  $('#sheetHint').textContent = content.hint;
  $('#sheetSubmit').textContent = content.submit;
  $('#sheetSubmit').classList.toggle('danger', action === 'reject' || action === 'logout');
  $('#sheetBackdrop').hidden = false;
  $('#actionSheet').hidden = false;
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => $('#actionSheet input, #actionSheet textarea, #sheetSubmit')?.focus());
}

function sheetContent(action, title) {
  const safeTitle = escapeHtml(title);
  const templates = {
    confirm: {
      kicker: '人工确认', title: `确认“${title}”`, submit: '确认并生成评分',
      fields: `<label class="field">题材名称<input name="final_name" value="${safeTitle}" maxlength="40" required></label>
        <label class="field">催化强度（0–100，可选）<input name="catalyst_strength" type="number" min="0" max="100" placeholder="例如 75"></label>
        <label class="field">催化持续时间（可选）<input name="catalyst_duration" maxlength="20" placeholder="例如 一周 / 数月"></label>`,
      hint: 'Day 1保持为机器首次发现共性异动的日期。'
    },
    reject: {
      kicker: '忽略候选', title: `忽略“${title}”`, submit: '确认忽略', fields: '',
      hint: '该操作不会删除历史记录，之后仍可在数据库中追溯。'
    },
    explain: {
      kicker: 'Theme Explanation Engine', title: `解释“${title}”`, submit: '搜索并更新解释', fields: '',
      hint: 'AI会搜索最近72小时催化，只负责命名、合并建议和新闻解释，不改变股票成员。'
    },
    merge: {
      kicker: '人工合并', title: `合并到“${title}”`, submit: '确认合并',
      fields: '<label class="field">来源题材 ID<input name="source_ids" placeholder="例如 12, 15" required></label>',
      hint: '输入需要合并进当前题材的候选ID，多个ID用逗号分隔。'
    },
    split: {
      kicker: '人工拆分', title: `拆分“${title}”`, submit: '创建新题材',
      fields: '<label class="field">股票代码<textarea name="member_codes" rows="3" placeholder="600000.SH, 000001.SZ" required></textarea></label><label class="field">新题材名称<input name="new_name" maxlength="40" required></label>',
      hint: '只允许从当前题材已有成员中拆分，股票成员不会由AI修改。'
    },
    wecom: {
      kicker: '通知通道', title: '测试企业微信', submit: '发送测试消息', fields: '',
      hint: '将向.env中配置的企业微信UserID发送一条连接测试消息。'
    },
    logout: {
      kicker: '当前会话', title: '退出 StockTopic', submit: '退出登录', fields: '',
      hint: '退出后会清除当前标签页保存的登录凭据，不影响后台采集。'
    }
  };
  return templates[action];
}

function closeSheet() {
  $('#sheetBackdrop').hidden = true;
  $('#actionSheet').hidden = true;
  document.body.style.overflow = '';
  state.lastFocus?.focus();
  state.sheet = null;
}

$('#sheetForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!state.sheet) return;
  const { action, theme } = state.sheet;
  const data = Object.fromEntries(new FormData(event.currentTarget));
  const submit = $('#sheetSubmit');
  submit.disabled = true;
  const originalText = submit.textContent;
  submit.textContent = '正在处理…';
  try {
    if (action === 'confirm') {
      await api(`/api/v1/themes/${theme.id}/confirm`, {
        method: 'POST',
        body: JSON.stringify({
          final_name: data.final_name.trim(),
          catalyst_strength: data.catalyst_strength ? Number(data.catalyst_strength) : null,
          catalyst_duration: data.catalyst_duration.trim() || null
        })
      });
    } else if (action === 'reject') {
      await api(`/api/v1/themes/${theme.id}/reject`, { method: 'POST' });
    } else if (action === 'explain') {
      await api(`/api/v1/themes/${theme.id}/explain`, { method: 'POST' });
    } else if (action === 'merge') {
      const sourceIds = data.source_ids.split(/[\s,]+/).map(value => Number(value.trim())).filter(Boolean);
      if (!sourceIds.length) throw new Error('请输入有效的来源题材ID');
      await api(`/api/v1/themes/${theme.id}/merge`, {
        method: 'POST', body: JSON.stringify({ source_ids: sourceIds })
      });
    } else if (action === 'split') {
      const memberCodes = data.member_codes.split(/[\s,]+/).map(value => value.trim()).filter(Boolean);
      await api(`/api/v1/themes/${theme.id}/split`, {
        method: 'POST',
        body: JSON.stringify({ member_codes: memberCodes, new_name: data.new_name.trim() })
      });
    } else if (action === 'wecom') {
      await api('/api/v1/admin/wecom-test', { method: 'POST' });
    } else if (action === 'logout') {
      closeSheet();
      showLogin();
      toast('已退出当前会话');
      return;
    }
    closeSheet();
    await load();
    toast(action === 'wecom' ? '企业微信测试消息已发送' : '操作已完成');
  } catch (error) {
    $('#sheetHint').textContent = error.message;
    toast(error.message, true);
  } finally {
    submit.disabled = false;
    submit.textContent = originalText;
  }
});

let toastTimer;
function toast(message, isError = false) {
  const node = $('#toast');
  $('#toastText').textContent = message;
  $('#toastIcon').textContent = isError ? '!' : '✓';
  node.classList.toggle('error', isError);
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 2800);
}

if (state.auth) {
  hideLogin();
  load();
} else {
  showLogin();
}

setInterval(() => {
  if (state.auth && document.visibilityState === 'visible') load();
}, 60_000);
