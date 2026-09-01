let savedSections = {};
let fundFlowPollTimer = null;
try { savedSections = JSON.parse(localStorage.getItem('stocktopicSections') || '{}'); }
catch (_) { savedSections = {}; }
const state = {
  data: null,
  authenticated: false,
  view: 'candidates',
  candidateFilter: 'watching',
  themeFilter: 'active',
  backtestFilter: 'all',
  level2Report: null,
  level2Context: null,
  sheet: null,
  lastFocus: null,
  expanded: savedSections
};

const views = {
  candidates: {
    kicker: '早期观察',
    title: '已经形成市场共识，等待证据确认',
    description: '主板触板与创业板涨幅超10%按上涨原因聚合；未入池候选保留完整审查原因。'
  },
  confirmed: {
    kicker: '重点题材库',
    title: '已经通过多源证据验证',
    description: '正式题材通过持续性审查与官方信息或多源产业证据交叉验证。'
  },
  backtest: {
    kicker: '测试票池',
    title: '验证题材股票的次日买入效果',
    description: '开盘后确认买入并实时跟踪涨跌，收盘日线校准后自动结算。'
  },
  alerts: {
    kicker: '系统预警',
    title: '重要机会与数据故障',
    description: '新题材、风险和数据拉取异常会记录企业微信群机器人送达状态。'
  }
};

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
}[character]));

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
    if (path !== '/api/v1/auth/login') showLogin('登录已失效，请重新输入密码。');
    throw new Error('用户名或密码不正确');
  }
  if (!response.ok) throw new Error(body.detail || `请求失败 ${response.status}`);
  return body;
}

function showLogin(message = '') {
  state.authenticated = false;
  $('#loginBackdrop').classList.remove('authenticated');
  $('#loginError').textContent = message;
  requestAnimationFrame(() => $('#loginPassword').focus());
}

function hideLogin() {
  state.authenticated = true;
  $('#loginBackdrop').classList.add('authenticated');
  $('#loginError').textContent = '';
  $('#loginPassword').value = '';
}

$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#loginSubmit');
  const credentials = {
    username: $('#loginUsername').value.trim(),
    password: $('#loginPassword').value
  };
  button.disabled = true;
  button.textContent = '正在验证…';
  $('#loginError').textContent = '';
  try {
    await api('/api/v1/auth/login', {
      method: 'POST', body: JSON.stringify(credentials)
    });
    state.data = await api('/api/v1/dashboard');
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

$$('[data-theme-filter]').forEach(button => button.addEventListener('click', () => {
  state.themeFilter = button.dataset.themeFilter;
  $$('[data-theme-filter]').forEach(item => item.classList.toggle('active', item === button));
  renderConfirmed();
  updateSectionCount();
}));

$$('[data-candidate-filter]').forEach(button => button.addEventListener('click', () => {
  state.candidateFilter = button.dataset.candidateFilter;
  $$('[data-candidate-filter]').forEach(item => item.classList.toggle('active', item === button));
  renderCandidates();
  updateSectionCount();
}));

$$('[data-backtest-filter]').forEach(button => button.addEventListener('click', () => {
  state.backtestFilter = button.dataset.backtestFilter;
  $$('[data-backtest-filter]').forEach(item => item.classList.toggle('active', item === button));
  renderBacktest();
  updateSectionCount();
}));

$('#refresh').addEventListener('click', () => load(true));
$('#accountButton').addEventListener('click', event => openSheet('logout', {}, event.currentTarget));
$('#wecomTest').addEventListener('click', event => openSheet('wecom', {}, event.currentTarget));
$('#sheetBackdrop').addEventListener('click', closeSheet);
$('#level2Backdrop').addEventListener('click', closeLevel2);
$$('[data-close-sheet]').forEach(button => button.addEventListener('click', closeSheet));
$$('[data-close-level2]').forEach(button => button.addEventListener('click', closeLevel2));
document.addEventListener('keydown', event => {
  const sheet = $('#actionSheet');
  const level2Sheet = $('#level2Sheet');
  if (event.key === 'Escape' && !level2Sheet.hidden) closeLevel2();
  if (event.key === 'Escape' && !sheet.hidden) closeSheet();
  if (event.key !== 'Tab' || sheet.hidden) return;
  const focusable = [...sheet.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled])')];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus();
  }
});

async function load(manual = false) {
  const refresh = $('#refresh');
  refresh.classList.add('loading');
  try {
    state.data = await api('/api/v1/dashboard');
    render();
    if (manual) toast('数据已刷新');
  } catch (error) {
    if (state.authenticated) toast(error.message, true);
  } finally {
    refresh.classList.remove('loading');
  }
}

function render() {
  if (!state.data) return;
  const { health, themes, alerts, backtest = { entries: [], summary: {} } } = state.data;
  const pending = themes.filter(theme => theme.status === 'pending');
  const watching = themes.filter(theme => theme.status === 'watching');
  const confirmed = themes.filter(theme => theme.status === 'confirmed');
  const market = health.market;
  const marketNode = $('#marketState');
  marketNode.classList.remove('loading', 'live', 'warning');
  marketNode.classList.add(market.realtime_collection_enabled ? 'live' : 'warning');
  marketNode.querySelector('strong').textContent = market.realtime_collection_enabled
    ? '盘中采集中' : marketLabel(market.session);
  marketNode.querySelector('small').textContent = market.realtime_collection_enabled
    ? '5分钟行情 · 5分钟涨停池'
    : health.latest_quote_run
      ? `最近采集 ${formatDataTime(health.latest_quote_run.started_at)}`
      : marketReason(market.reason);
  $('#universeCount').textContent = health.universe_count?.toLocaleString() || '—';
  $('#candidateMetric').textContent = watching.length.toLocaleString();
  $('#confirmedMetric').textContent = confirmed.length.toLocaleString();
  $('#lastRun').textContent = formatTime(health.latest_quote_run?.started_at);
  $('#lastRunDetail').textContent = runStatus(health.latest_quote_run?.status);
  $('#candidateCount').textContent = pending.length + watching.length;
  $('#confirmedCount').textContent = confirmed.length;
  $('#backtestCount').textContent = backtest.entries.length;
  $('#alertCount').textContent = alerts.length;
  renderCandidates();
  renderConfirmed();
  renderBacktest();
  renderAlerts(alerts);
  updateSectionCount();
  scheduleFundFlowPoll(themes, backtest.entries || []);
}

function renderCandidates() {
  if (!state.data) return;
  let items = state.data.themes.filter(theme =>
    ['pending', 'watching', 'rejected'].includes(theme.status)
  );
  if (state.candidateFilter === 'watching') {
    items = items.filter(theme => theme.status === 'watching');
  } else if (state.candidateFilter === 'reviewing') {
    items = items.filter(theme => theme.status === 'pending');
  } else if (state.candidateFilter === 'rejected') {
    items = items.filter(theme => theme.status === 'rejected');
  }
  const priority = { watching: 0, pending: 1, rejected: 2 };
  items.sort((a, b) => (priority[a.status] ?? 9) - (priority[b.status] ?? 9));
  renderThemes('#candidateList', items, 'audit');
}

function renderConfirmed() {
  if (!state.data) return;
  const themes = state.data.themes;
  let items = themes.filter(theme => theme.status === 'confirmed');
  let mode = 'confirmed';
  if (state.themeFilter === 'pinned') items = items.filter(theme => theme.pinned);
  if (state.themeFilter === 'archived') {
    items = themes.filter(theme => theme.status === 'archived');
    mode = 'archived';
  }
  renderThemes('#confirmedList', items, mode);
}

function renderThemes(selector, items, mode) {
  const root = $(selector);
  root.classList.remove('skeleton-list');
  const audit = mode === 'audit';
  root.innerHTML = items.length
    ? items.map(theme => audit && theme.status === 'rejected'
      ? rejectedThemeDisclosure(theme, mode)
      : themeCard(theme, mode)).join('')
    : emptyState(
      audit ? '当前筛选下没有记录' : mode === 'archived' ? '没有已归档题材' : '暂无重点题材',
      audit
        ? '所有达到4票共同事件门槛的候选都会保留审查状态和未入池原因。'
        : '通过新颖性、催化和持续性审查后才会自动入库。'
    );
  root.querySelectorAll('[data-action]').forEach(button => {
    button.addEventListener('click', async event => {
      const theme = items.find(item => Number(item.id) === Number(button.dataset.id));
      if (!theme) return;
      if (button.dataset.action === 'track-stock') {
        await trackStock(theme, button.dataset.code, button);
      } else if (button.dataset.action === 'level2-stock') {
        await analyzeLevel2(theme, button.dataset.code, button);
      } else if (button.dataset.action === 'toggle-pin' || button.dataset.action === 'restore') {
        await immediateThemeAction(button.dataset.action, theme, button);
      } else {
        openSheet(button.dataset.action, theme, event.currentTarget);
      }
    });
  });
  root.querySelectorAll('details[data-collapse-key]').forEach(details => {
    details.addEventListener('toggle', () => {
      state.expanded[details.dataset.collapseKey] = details.open;
      localStorage.setItem('stocktopicSections', JSON.stringify(state.expanded));
    });
  });
}

function rejectedThemeDisclosure(theme, mode) {
  const title = theme.final_name || theme.suggested_name || theme.provisional_name || '未命名题材';
  const collapseKey = `rejected-theme-${theme.id}`;
  return `<details class="rejected-theme-disclosure" data-collapse-key="${collapseKey}" ${state.expanded[collapseKey] ? 'open' : ''}>
    <summary><strong>${escapeHtml(title)}</strong><i aria-hidden="true">⌄</i></summary>
    <div class="rejected-theme-content">${themeCard(theme, mode)}</div>
  </details>`;
}

function themeCard(theme, mode) {
  const pending = theme.status === 'pending';
  const watching = theme.status === 'watching';
  const rejected = theme.status === 'rejected';
  const archived = mode === 'archived';
  const title = theme.final_name || theme.suggested_name || theme.provisional_name;
  const activeMembers = theme.members.filter(member => member.active);
  const returnLabel = pending || watching || rejected ? '触发后' : '入库后';
  const backtest = state.data?.backtest || { entries: [] };
  const trackedCodes = new Set(backtest.entries
    .filter(item => item.signal_trade_date === backtest.current_signal_trade_date)
    .map(item => item.code));
  const members = activeMembers.map((member, index) => memberRow(
    member, index + 1, returnLabel, theme.id, trackedCodes.has(member.code)
  )).join('');
  const score = theme.score;
  const summary = theme.market_summary || {};
  const review = theme.admission_review;
  const statusHtml = `<div class="theme-stage ${escapeHtml(theme.theme_level || theme.status)}">
    <span>${escapeHtml(themeStageLabel(theme))}</span>
    <small>${escapeHtml(stageDetail(theme))}</small>
  </div>`;
  const scoreHtml = score ? `<div class="score-grid">
    ${scoreCell('热度', score.heat, 'heat')}
    ${scoreCell('持续性', score.persistence, 'persistence')}
    ${scoreCell('接盘风险', score.entry_risk, 'risk')}
  </div><div class="lifecycle-row">
    <span class="lifecycle-chip">${escapeHtml(score.lifecycle)} · Day ${escapeHtml(score.details.day_number)}</span>
    ${score.leader_theme_divergence ? '<span class="risk-note">龙头—板块背离</span>' : ''}
  </div>` : `<div class="locked-score"><span class="lock-icon">◇</span><span>${escapeHtml(admissionLabel(theme.admission_status))}</span></div>`;
  const reviewHtml = review ? `<div class="admission-strip">
    ${summaryMetric('新颖性', `${Number(review.novelty_confidence).toFixed(0)}`)}
    ${summaryMetric('催化可信度', `${Number(review.catalyst_confidence).toFixed(0)}`)}
    ${summaryMetric('预估持续', `${review.expected_duration_days}日`)}
    ${summaryMetric('龙头情景空间', formatPct(review.leader_upside_scenario_pct))}
  </div>` : '';
  const decisionHtml = theme.admission_reason ? `<div class="decision-note ${rejected ? 'rejected' : watching ? 'watching' : ''}">
    <strong>${rejected ? '未入池原因' : watching ? '当前证据状态' : '审查进度'}</strong>
    <p>${escapeHtml(theme.admission_reason)}</p>
  </div>` : '';
  const summaryHtml = `<div class="market-summary">
    ${summaryMetric('当前平均', formatPct(summary.current_average_pct))}
    ${summaryMetric(`${returnLabel}平均`, formatPct(summary.confirmed_average_return))}
    ${summaryMetric('上涨家数', `${summary.up_count ?? 0}/${summary.member_count ?? activeMembers.length}`)}
    ${summaryMetric('涨停 / 创业板强势 / 炸板', `${summary.limit_up_count ?? 0} / ${summary.chinext_growth_count ?? 0} / ${summary.failed_limit_count ?? 0}`)}
  </div>`;
  const fundFlowHtml = themeFundFlow(theme);
  const stockKey = `theme-${theme.id}-stocks`;
  const newsKey = `theme-${theme.id}-news`;
  const stockDetails = `<details class="fold-section" data-collapse-key="${stockKey}" ${state.expanded[stockKey] ? 'open' : ''}>
    <summary><span><strong>题材股票行情榜</strong><small>按当前涨幅纵向排序 · ${activeMembers.length}只</small></span><i>⌄</i></summary>
    <div class="member-table" role="table" aria-label="${escapeHtml(title)}股票行情">
      <div class="member-table-head" role="row"><span>股票</span><span>当前</span><span>${returnLabel}</span><span>近期连板</span><span>流通市值</span><span>带动</span><span>资金 / 操作</span></div>
      <div class="member-table-body">${members}</div>
    </div>
  </details>`;
  const catalysts = theme.catalysts || [];
  const explanation = theme.latest_explanation;
  const newsDetails = (catalysts.length || explanation) ? `<details class="fold-section catalyst-fold" data-collapse-key="${newsKey}" ${state.expanded[newsKey] ? 'open' : ''}>
    <summary><span><strong>新闻催化</strong><small>定时更新 · ${catalysts.length}条</small></span><i>⌄</i></summary>
    ${catalystContent(catalysts, explanation)}
  </details>` : '';
  let actions = '';
  if ((watching || theme.status === 'confirmed') && !archived) {
    actions = `<div class="card-actions">
      <button class="secondary-button pressable" data-action="toggle-pin" data-id="${theme.id}">${theme.pinned ? '取消置顶' : '置顶'}</button>
      <button class="secondary-button pressable" data-action="explain" data-id="${theme.id}">更新新闻催化</button>
      <button class="text-button danger pressable" data-action="archive" data-id="${theme.id}">移除并归档</button>
    </div>`;
  } else if (archived) {
    actions = `<div class="card-actions"><button class="secondary-button pressable" data-action="restore" data-id="${theme.id}">恢复到题材库</button></div>`;
  }
  return `<article class="theme-card ${theme.pinned ? 'pinned-theme' : ''} stage-${escapeHtml(theme.theme_level || theme.status)}">
    <div class="theme-head"><div><div class="theme-title">${theme.pinned ? '<span class="pin-mark">置顶</span>' : ''}${escapeHtml(title)}</div><p>${escapeHtml(theme.discovery_reason)}</p></div><span class="theme-tag">${escapeHtml(theme.shared_tag)}</span></div>
    ${statusHtml}${scoreHtml}${reviewHtml}${decisionHtml}${summaryHtml}${fundFlowHtml}${stockDetails}
    <p class="theme-footnote">Day 1 ${escapeHtml(theme.day1_date)} · 行情 ${escapeHtml(formatDataTime(summary.market_data_at))} · 市值 ${escapeHtml(formatDate(summary.metric_trade_date))}</p>
    ${newsDetails}${actions}
  </article>`;
}

function memberRow(member, position, returnLabel, themeId, tracked) {
  const evidence = member.evidence || {};
  const reasons = evidence.aggregated_reason || evidence.limit_reason || evidence.ai_reason || evidence.shared_tag || '同题材确定性证据';
  const current = Number(member.current_pct);
  const cumulative = member.confirmed_return;
  const leader = Number(member.leader_rank) === 1;
  const boardHistory = (member.board_history || [])
    .map(item => `${formatDate(item.trade_date)} ${item.status || item.tag || '普通'}`).join(' · ');
  const board = member.board_status || (member.latest_board_tag ? `近期${member.latest_board_tag}` : '—');
  const sequence = member.limit_sequence ? `第${member.limit_sequence}封板` : '';
  const follow = Number(member.follow_count_30m || 0);
  const flowTrigger = memberFundFlowTrigger(member, themeId);
  return `<div class="member-row ${leader ? 'leader-row' : ''}" role="row" title="${escapeHtml(reasons)}">
    <div class="member-name" role="cell"><span class="member-rank">${position}</span><span><strong>${escapeHtml(member.name)}</strong><small>${escapeHtml(member.code)}</small></span>${leader ? '<b class="leader-badge">龙头候选</b>' : ''}</div>
    <div class="member-number ${valueClass(current)}" role="cell"><strong>${formatPct(current)}</strong><small>${escapeHtml(formatPrice(member.current_price))}</small></div>
    <div class="member-number ${valueClass(cumulative)}" role="cell"><strong>${formatPct(cumulative)}</strong><small>${escapeHtml(returnLabel)}累计</small></div>
    <div class="board-cell" role="cell" title="${escapeHtml(boardHistory)}"><strong>${escapeHtml(board)}</strong><small>${escapeHtml([sequence, timeLabel(member.first_limit_time)].filter(Boolean).join(' · ') || '近5日记录')}</small></div>
    <div class="member-number neutral" role="cell"><strong>${formatMarketCap(member.circ_mv_billion)}</strong><small>${member.turnover_rate != null ? `换手 ${Number(member.turnover_rate).toFixed(1)}%` : '亿元'}</small></div>
    <div class="drive-cell" role="cell"><strong>${follow}</strong><small>30分钟跟随</small></div>
    <div class="track-cell" role="cell">${flowTrigger}<button class="track-button pressable" data-action="track-stock" data-id="${themeId}" data-code="${escapeHtml(member.code)}" ${tracked ? 'disabled' : ''}>${tracked ? '已跟踪' : '跟踪'}</button></div>
  </div>`;
}

function memberFundFlowTrigger(member, themeId) {
  const flow = member.fund_flow || { status: 'pending' };
  const summary = flow.summary || {};
  const large = summary.large_net_inflow;
  const superLarge = summary.super_net_inflow;
  const largeText = large == null ? '—' : formatFlowMoney(large, true);
  const superText = superLarge == null ? '—' : formatFlowMoney(superLarge, true);
  const status = flow.status || 'pending';
  const statusText = fundFlowStatusLabel(status, true);
  const title = flow.error || `${statusText} · 点击查看主动委托明细`;
  const aria = `${member.name || member.code}资金明细，50万以上净流入${largeText}，100万以上净流入${superText}`;
  return `<button class="flow-net-trigger pressable status-${escapeHtml(status)}" data-action="level2-stock" data-id="${themeId}" data-code="${escapeHtml(member.code)}" title="${escapeHtml(title)}" aria-label="${escapeHtml(aria)}">
    <span><small>50W+</small><strong class="${large == null ? 'empty' : valueClass(large)}">${escapeHtml(largeText)}</strong></span>
    <span><small>100W+</small><strong class="${superLarge == null ? 'empty' : valueClass(superLarge)}">${escapeHtml(superText)}</strong></span>
    <i>${escapeHtml(statusText)}</i>
  </button>`;
}

function themeFundFlow(theme) {
  const flow = theme.fund_flow;
  if (!flow || !['watching', 'confirmed', 'archived'].includes(theme.status)) return '';
  const summary = flow.summary;
  const progress = `${flow.completed_count || 0}/${flow.target_count || 0}`;
  const slot = flow.slot === 'close' ? '收盘后' : '10:00';
  const failed = Number(flow.failed_count || 0);
  const detail = flow.status === 'stopped'
    ? '题材已移除，不再更新池内股票'
    : `${slot} · TOP5 ${progress}${failed ? ` · ${failed}只暂未取得数据` : ''}`;
  const metrics = summary ? `<div class="fund-flow-metrics">
    ${fundFlowMetric('TOP5 · 50W+净流入', formatFlowMoney(summary.large?.net_inflow, true), valueClass(summary.large?.net_inflow))}
    ${fundFlowMetric('TOP5 · 100W+净流入', formatFlowMoney(summary.super_large?.net_inflow, true), valueClass(summary.super_large?.net_inflow))}
  </div>` : '';
  return `<section class="theme-fund-flow status-${escapeHtml(flow.status)}" aria-live="polite">
    <div class="fund-flow-head"><span class="flow-status-dot" aria-hidden="true"></span><div><strong>${escapeHtml(fundFlowStatusLabel(flow.status))}</strong><small>${escapeHtml(detail)}</small></div></div>
    ${metrics}
  </section>`;
}

function fundFlowMetric(label, value, className = '') {
  return `<div class="${escapeHtml(className)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function fundFlowStatusLabel(status, short = false) {
  if (status === 'running') return short ? '资金更新中' : '资金流向更新中';
  if (status === 'completed') return short ? '资金已更新' : '资金流向已更新';
  if (status === 'stopped') return short ? '资金已停止' : '资金流向已停止';
  return short ? '资金未更新' : '资金流向未更新';
}

async function analyzeLevel2(theme, code, button, forceRefresh = false) {
  const member = (theme.members || []).find(item => item.code === code) || {};
  openLevel2(member.name || code);
  state.level2Context = { theme, code };
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    const result = await api('/api/v1/level2/analyze', {
      method: 'POST', body: JSON.stringify({ code, force_refresh: forceRefresh })
    });
    state.level2Report = result.report;
    renderLevel2Report(result.report);
  } catch (error) {
    $('#level2Content').innerHTML = emptyState('Level-2分析失败', error.message);
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

function openLevel2(name) {
  state.level2Report = null;
  $('#level2Title').textContent = `${name} · 主动委托资金`;
  $('#level2Content').innerHTML = '<div class="level2-loading"><span></span><strong>正在分页读取逐笔成交与委托…</strong><small>按主动方订单号合并，不按单笔成交金额筛选</small></div>';
  $('#level2Backdrop').hidden = false;
  $('#level2Sheet').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeLevel2() {
  $('#level2Backdrop').hidden = true;
  $('#level2Sheet').hidden = true;
  document.body.style.overflow = '';
}

function renderLevel2Report(report) {
  $('#level2Title').textContent = `${report.name} · ${formatDate(report.trade_date)}`;
  const thresholds = (report.thresholds || []).map(item => {
    const ratio = item.buy_ratio_pct;
    const width = ratio == null ? 0 : Math.max(0, Math.min(100, Number(ratio)));
    return `<article class="flow-tier">
      <div><strong>${escapeHtml(item.label)}</strong><span>买入 ${ratio == null ? '—' : `${Number(ratio).toFixed(0)}%`}</span></div>
      <div class="flow-bar"><i style="width:${width}%"></i></div>
      <small>主动买 ${escapeHtml(formatFlowMoney(item.buy_amount))} / ${item.buy_order_count}单 · 主动卖 ${escapeHtml(formatFlowMoney(item.sell_amount))} / ${item.sell_order_count}单</small>
    </article>`;
  }).join('');
  const tierMap = Object.fromEntries((report.thresholds || []).map(item => [item.label, item]));
  const coverage = report.coverage || {};
  const events = (report.events || []).slice(0, 12).map(item => `<li>
    <span class="flow-event-side ${item.direction}">${item.direction === 'buy' ? '买' : '卖'}</span>
    <div><strong>${escapeHtml(item.event_label)} · ${escapeHtml(formatFlowMoney(item.amount))}</strong><small>${escapeHtml(item.first_time || '时间未知')} · ${item.fill_count}笔成交 · 委托号 ${escapeHtml(item.order_id)}</small></div>
  </li>`).join('');
  const profile = report.raw_profile || {};
  $('#level2Content').innerHTML = `
    ${report.partial ? '<div class="partial-note">盘中数据 · 收盘前结果仍会变化</div>' : ''}
    ${report.cache_hit ? '<div class="cache-note">已读取本地完整报告 · 无需重复下载近10万条明细</div>' : ''}
    <div class="flow-tiers">${thresholds}</div>
    <div class="flow-net-grid">
      ${flowNet('大单净主动流入', tierMap['50W+']?.net_inflow)}
      ${flowNet('超大单净主动流入', tierMap['100W+']?.net_inflow)}
    </div>
    <div class="coverage-note"><strong>计算可信度</strong><span>主动方向覆盖 ${Number(coverage.directional_amount_coverage_pct || 0).toFixed(1)}% · 委托号覆盖 ${Number(coverage.order_id_amount_coverage_pct || 0).toFixed(1)}%</span><small>${coverage.grouped_trade_count || 0}笔成交已归并为${coverage.active_order_count || 0}个主动委托</small></div>
    <details class="flow-events" open><summary>50万以上主动委托明细 <span>${(report.events || []).length}</span></summary><ol>${events || '<li class="no-flow-event">没有达到50万元的可识别主动委托</li>'}</ol></details>
    <details class="raw-profile"><summary>原始字段映射审计</summary><pre>${escapeHtml(JSON.stringify(profile, null, 2))}</pre></details>
    <p class="level2-limit">${escapeHtml((report.limitations || []).join('；'))}</p>
    <button id="level2ForceRefresh" class="secondary-button pressable">重新下载最新数据</button>`;
  $('#level2ForceRefresh').addEventListener('click', event => {
    const context = state.level2Context;
    if (context) analyzeLevel2(context.theme, context.code, event.currentTarget, true);
  });
}

function flowNet(label, value) {
  const number = Number(value || 0);
  return `<article><span>${escapeHtml(label)}</span><strong class="${valueClass(number)}">${escapeHtml(formatFlowMoney(number, true))}</strong></article>`;
}

function formatFlowMoney(value, signed = false) {
  const number = Number(value || 0);
  const sign = signed && number > 0 ? '+' : '';
  const absolute = Math.abs(number);
  const prefix = number < 0 ? '-' : sign;
  if (absolute >= 1e8) return `${prefix}${(absolute / 1e8).toFixed(2)}亿`;
  if (absolute >= 1e4) return `${prefix}${(absolute / 1e4).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}万`;
  return `${prefix}${absolute.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}元`;
}

async function trackStock(theme, code, button) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = '记录中…';
  try {
    const result = await api('/api/v1/test-pool', {
      method: 'POST', body: JSON.stringify({ theme_id: Number(theme.id), code })
    });
    toast(result.created ? '已加入测试票池' : '本交易日已跟踪，题材来源已合并');
    await load();
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    toast(error.message, true);
  }
}

function renderBacktest() {
  if (!state.data) return;
  const backtest = state.data.backtest || { entries: [], summary: {} };
  const summary = backtest.summary || {};
  $('#backtestSummary').innerHTML = [
    backtestMetric('有效样本', summary.completed_count ?? 0, `${summary.success_count ?? 0}成功 · ${summary.failure_count ?? 0}失败`),
    backtestMetric('成功率', formatPct(summary.success_rate), `持平 ${summary.flat_count ?? 0} 笔不计成败`),
    backtestMetric('等权标准收益', formatPct(summary.average_standard_return_pct), 'T+1开盘买 · T+2开盘卖'),
    backtestMetric('最高可能收益', formatPct(summary.average_maximum_return_pct), 'T+2最高价理论上限'),
    backtestMetric('待结算 / 未成交', `${summary.pending_count ?? 0} / ${summary.unfilled_count ?? 0}`, '未成交不进入收益统计')
  ].join('');
  let entries = [...(backtest.entries || [])];
  if (state.backtestFilter === 'pending') {
    entries = entries.filter(item => ['awaiting_buy', 'awaiting_exit', 'awaiting_settlement'].includes(item.status));
  } else if (state.backtestFilter === 'completed') {
    entries = entries.filter(item => ['success', 'failure', 'flat'].includes(item.status));
  } else if (state.backtestFilter === 'unfilled') {
    entries = entries.filter(item => ['unfilled', 'invalid'].includes(item.status));
  }
  $('#backtestList').innerHTML = entries.length
    ? entries.map(backtestRow).join('')
    : emptyState('当前筛选下没有测试记录', '在题材股票行情榜点击“跟踪”即可建立一笔独立样本。');
}

function backtestMetric(label, value, detail) {
  return `<article><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`;
}

function backtestRow(item) {
  const sources = (item.source_themes || []).map(source =>
    `<span>${escapeHtml(source.name)}</span>`).join('');
  const status = backtestStatus(item.status);
  const exitDate = item.actual_exit_date || item.exit_attempt_date || item.planned_exit_date;
  const delay = Number(item.exit_delay_trade_days || 0);
  const holding = item.status === 'awaiting_exit';
  const soldPending = item.status === 'awaiting_settlement';
  const liveTime = item.live_updated_at ? `更新 ${formatTime(item.live_updated_at)}` : '等待实时行情';
  const standardValue = holding ? item.current_return_pct : item.standard_return_pct;
  const maximumValue = holding ? item.current_high_return_pct : item.maximum_return_pct;
  const source = item.buy_confirmation_source === 'realtime_rt_k'
    ? '盘中确认 · 待日线校准'
    : item.buy_confirmation_source === 'official_daily' ? '正式日线已确认' : formatDate(item.planned_buy_date);
  const flow = item.fund_flow || { status: 'pending' };
  const flowSummary = flow.summary;
  const flowDetail = flowSummary
    ? `50W+买入 ${flowSummary.large_buy_ratio_pct == null ? '—' : `${Number(flowSummary.large_buy_ratio_pct).toFixed(0)}%`} · 净 ${formatFlowMoney(flowSummary.large_net_inflow, true)}`
    : flow.status === 'stopped' ? '已卖出或未成交，不再更新' : flow.error || (flow.slot === 'close' ? '等待收盘后任务' : '等待10:00任务');
  return `<article class="backtest-row status-${escapeHtml(item.status)}">
    <div class="backtest-stock"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)} · 信号 ${escapeHtml(formatDate(item.signal_trade_date))}</small><div>${sources}</div><p class="backtest-flow-state ${escapeHtml(flow.status)}"><span></span>${escapeHtml(fundFlowStatusLabel(flow.status))}<small>${escapeHtml(flowDetail)}</small></p></div>
    <div class="backtest-step"><span>T+1 买入</span><strong>${escapeHtml(formatPrice(item.buy_open))}</strong><small>${escapeHtml(source)}</small></div>
    <div class="backtest-step"><span>${delay ? '顺延退出' : 'T+2 开盘'}</span><strong>${escapeHtml(formatPrice(item.exit_open))}</strong><small>${escapeHtml(formatDate(exitDate))}${delay ? ` · 延迟${delay}日` : ''}</small></div>
    <div class="backtest-return ${valueClass(standardValue)}"><span>${holding ? '持仓涨跌' : '标准收益'}</span><strong>${escapeHtml(formatPct(standardValue))}</strong><small>${holding ? `${formatPrice(item.current_price)} · ${liveTime}` : soldPending ? `盘中确认 · ${liveTime}` : '开盘卖出'}</small></div>
    <div class="backtest-return ${valueClass(maximumValue)}"><span>${holding ? '持仓最高' : soldPending ? '当日最高' : '最高可能'}</span><strong>${escapeHtml(formatPct(maximumValue))}</strong><small>${holding && item.current_high ? `最高 ¥${Number(item.current_high).toFixed(2)}` : item.exit_high ? `最高 ¥${Number(item.exit_high).toFixed(2)}` : '等待最高价'}</small></div>
    <div class="backtest-outcome"><span class="outcome-badge ${escapeHtml(item.status)}">${escapeHtml(status)}</span><small>${escapeHtml(item.status_reason || backtestStatusDetail(item.status))}</small></div>
  </article>`;
}

function backtestStatus(status) {
  return ({ awaiting_buy: '待买入', awaiting_exit: '持有中', awaiting_settlement: '已卖出·待校准', success: '成功', failure: '失败',
    flat: '持平', unfilled: '未成交', invalid: '数据无效' })[status] || '待处理';
}

function backtestStatusDetail(status) {
  return ({ awaiting_buy: '等待T+1开盘行情', awaiting_exit: '盘中跟踪，等待退出日结算', awaiting_settlement: 'T+2开盘已卖出，等待正式日线', success: '开盘卖出高于买入价',
    failure: '开盘卖出低于买入价', flat: '开盘卖出等于买入价', unfilled: '不计入统计' })[status] || '';
}

function scheduleFundFlowPoll(themes, entries) {
  if (fundFlowPollTimer) clearTimeout(fundFlowPollTimer);
  const updating = themes.some(theme => theme.fund_flow?.status === 'running')
    || entries.some(item => item.fund_flow?.status === 'running');
  if (updating) fundFlowPollTimer = setTimeout(() => load(), 15000);
}

function catalystContent(items, explanation) {
  const rows = items.map(item => {
    const url = safeUrl(item.source_url);
    const title = url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a>`
      : `<strong>${escapeHtml(item.title)}</strong>`;
    return `<li><div><span class="catalyst-type">${escapeHtml(item.catalyst_type || '更新')}</span><span class="evidence-level">${escapeHtml(item.evidence_level || '合理推断')}</span><time>${escapeHtml(formatCatalystTime(item.published_at || item.captured_at))}</time></div>${title}<p>${escapeHtml(item.summary)}</p></li>`;
  }).join('');
  const summary = explanation?.catalyst_summary
    ? `<p class="catalyst-summary">${escapeHtml(explanation.catalyst_summary)}</p>` : '';
  return `<div class="catalyst-panel">${summary}<ol>${rows}</ol></div>`;
}

async function immediateThemeAction(action, theme, button) {
  button.disabled = true;
  try {
    if (action === 'toggle-pin') {
      await api(`/api/v1/themes/${theme.id}/pin`, {
        method: 'POST', body: JSON.stringify({ pinned: !Boolean(theme.pinned) })
      });
      toast(theme.pinned ? '已取消置顶' : '已置顶');
    } else if (action === 'restore') {
      await api(`/api/v1/themes/${theme.id}/restore`, { method: 'POST' });
      toast('题材已恢复');
    }
    await load();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function openSheet(action, theme, trigger) {
  const title = theme.final_name || theme.suggested_name || theme.provisional_name || '';
  const content = sheetContent(action, title);
  if (!content) return;
  state.sheet = { action, theme };
  state.lastFocus = trigger;
  $('#sheetKicker').textContent = content.kicker;
  $('#sheetTitle').textContent = content.title;
  $('#sheetFields').innerHTML = content.fields || '';
  $('#sheetHint').textContent = content.hint || '';
  $('#sheetSubmit').textContent = content.submit;
  $('#sheetSubmit').classList.toggle('danger', action === 'archive');
  $('#sheetBackdrop').hidden = false;
  $('#actionSheet').hidden = false;
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => $('#sheetSubmit').focus());
}

function sheetContent(action, title) {
  return {
    explain: {
      kicker: '新闻催化', title: `更新“${title}”`, submit: '搜索并更新',
      hint: '将搜索近期及海外隔夜催化，保存来源、时间和证据等级。'
    },
    archive: {
      kicker: '移除题材', title: `归档“${title}”`, submit: '移除并归档',
      hint: '题材会从当前列表移除，历史数据保留，可在“已归档”中恢复。'
    },
    wecom: {
      kicker: '通知通道', title: '测试企业微信群机器人', submit: '发送测试消息',
      hint: '失败时会显示取Token、可信IP、接收账号或网络阶段的具体errcode。'
    },
    logout: {
      kicker: '当前账户', title: '退出 StockTopic', submit: '退出登录',
      hint: '将清除本浏览器的安全登录会话，不影响后台采集。'
    }
  }[action];
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
  const submit = $('#sheetSubmit');
  const originalText = submit.textContent;
  submit.disabled = true;
  submit.textContent = '正在处理…';
  try {
    if (action === 'explain') {
      await api(`/api/v1/themes/${theme.id}/explain`, { method: 'POST' });
    } else if (action === 'archive') {
      await api(`/api/v1/themes/${theme.id}/archive`, { method: 'POST' });
    } else if (action === 'wecom') {
      await api('/api/v1/admin/wecom-test', { method: 'POST' });
    } else if (action === 'logout') {
      await api('/api/v1/auth/logout', { method: 'POST' });
      closeSheet(); showLogin(); toast('已退出当前账户'); return;
    }
    closeSheet();
    await load();
    toast(action === 'wecom' ? '群机器人测试消息已送达' : '操作已完成');
  } catch (error) {
    $('#sheetHint').textContent = error.message;
    toast(error.message, true);
  } finally {
    submit.disabled = false;
    submit.textContent = originalText;
  }
});

function renderAlerts(items) {
  $('#alertList').innerHTML = items.length ? items.map(item => {
    const delivery = item.pushed_wecom
      ? '<span class="delivery-ok">群机器人已送达</span>'
      : item.push_error
        ? `<span class="delivery-failed" title="${escapeHtml(item.push_error)}">群机器人失败</span>`
        : '<span class="delivery-pending">未推送</span>';
    return `<article class="event-card">
      <time class="event-time">${escapeHtml(formatDataTime(item.created_at))}</time>
      <div class="event-main"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.body)}</small>${item.push_error ? `<p class="push-error">${escapeHtml(item.push_error)}</p>` : ''}</div>
      <div class="alert-status"><span class="event-badge ${item.severity === 'critical' ? 'critical' : 'high'}">${escapeHtml(item.severity)}</span>${delivery}</div>
    </article>`;
  }).join('') : emptyState('暂无预警', '没有新重点题材、风险事件或数据故障。');
}

function updateSectionCount() {
  if (!state.data) return;
  const { themes, alerts, backtest = { entries: [] } } = state.data;
  let count = 0;
  if (state.view === 'candidates') {
    const map = {
      watching: ['watching'], reviewing: ['pending'], rejected: ['rejected'],
      all: ['watching', 'pending', 'rejected']
    };
    count = themes.filter(theme => (map[state.candidateFilter] || map.all).includes(theme.status)).length;
  }
  if (state.view === 'confirmed') {
    count = state.themeFilter === 'archived'
      ? themes.filter(theme => theme.status === 'archived').length
      : themes.filter(theme => theme.status === 'confirmed' && (state.themeFilter !== 'pinned' || theme.pinned)).length;
  }
  if (state.view === 'alerts') count = alerts.length;
  if (state.view === 'backtest') {
    const entries = backtest.entries || [];
    if (state.backtestFilter === 'pending') count = entries.filter(item => ['awaiting_buy', 'awaiting_exit', 'awaiting_settlement'].includes(item.status)).length;
    else if (state.backtestFilter === 'completed') count = entries.filter(item => ['success', 'failure', 'flat'].includes(item.status)).length;
    else if (state.backtestFilter === 'unfilled') count = entries.filter(item => ['unfilled', 'invalid'].includes(item.status)).length;
    else count = entries.length;
  }
  $('#sectionCount').textContent = count;
}

function admissionLabel(status) {
  return ({
    awaiting_ai: '等待AI准入审查', analyzing: '正在分析新颖性、催化与持续性',
    analysis_failed: 'AI分析失败，已保留记录并等待重试',
    early_watch: '早期观察 · 证据待确认',
    admitted: '正式题材 · 已通过证据验证',
    not_admitted: '未达到题材准入条件'
  })[status] || '等待准入证据';
}

function themeStageLabel(theme) {
  return ({
    early_watch: '早期观察', formal: '正式题材', rejected: '未入池',
    candidate: 'AI审查中'
  })[theme.theme_level] || admissionLabel(theme.admission_status);
}

function stageDetail(theme) {
  if (theme.theme_level === 'early_watch') return '市场共识成立 · 官方或多源证据待确认';
  if (theme.theme_level === 'formal') return '持续性通过 · 证据已交叉验证';
  if (theme.theme_level === 'rejected') return '达到4票门槛，但未通过后续审查';
  if (theme.cluster_method === 'semantic_event') return '共同事件语义归并 · 正在分析';
  return '共同标签回退归并 · 正在分析';
}

function marketLabel(session) {
  return ({ opening_auction: '集合竞价', morning: '上午交易', afternoon: '下午交易',
    lunch_break: '午间休市', auction_gap: '竞价间隙', pre_market: '盘前待机', closed: '已收市'
  })[session] || '系统待机';
}

function marketReason(reason) {
  return ({ exchange_closed: '交易所休市 · 不采集', calendar_unknown_fail_closed: '日历未知 · 已停止采集',
    lunch_break: '午休期间不采集', auction_gap: '09:25–09:30不采集',
    pre_market: '等待交易窗口', closed: '等待下个交易窗口'
  })[reason] || '实时采集已停止';
}

function runStatus(status) {
  return ({ success: '采集正常', failed: '采集失败', degraded: '降级运行' })[status] || '等待行情';
}

function scoreCell(label, value, className) {
  const score = Math.max(0, Math.min(100, Number(value) || 0));
  return `<div class="score-cell ${className}"><span>${label}</span><strong>${escapeHtml(value)}</strong><div class="score-track"><i style="width:${score}%"></i></div></div>`;
}

function summaryMetric(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function formatPct(value) {
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '—';
  const number = Number(value);
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`;
}

function valueClass(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return 'neutral';
  return number > 0 ? 'rise' : 'fall';
}

function formatPrice(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? `¥${number.toFixed(2)}` : '价格待采集';
}

function formatMarketCap(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return '—';
  return number >= 1000 ? `${(number / 1000).toFixed(2)}千亿` : `${number.toFixed(number >= 100 ? 0 : 1)}亿`;
}

function timeLabel(value) {
  const digits = String(value || '').replace(/\D/g, '').padStart(6, '0');
  return digits && digits !== '000000' ? `${digits.slice(0, 2)}:${digits.slice(2, 4)}` : '';
}

function formatTime(value) {
  if (!value) return '—';
  const match = String(value).match(/T?(\d{2}):(\d{2})/);
  return match ? `${match[1]}:${match[2]}` : '—';
}

function formatDate(value) {
  const match = String(value || '').match(/(\d{4})-?(\d{2})-?(\d{2})/);
  return match ? `${match[2]}-${match[3]}` : '—';
}

function formatDataTime(value) {
  if (!value) return '待采集';
  return `${formatDate(value)} ${formatTime(value)}`;
}

function formatCatalystTime(value) {
  return value ? formatDataTime(value) : '时间待确认';
}

function safeUrl(value) {
  try {
    const url = new URL(String(value || ''));
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch (_) { return ''; }
}

function emptyState(title, detail) {
  return `<div class="empty-state"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

let toastTimer;
function toast(message, isError = false) {
  const node = $('#toast');
  $('#toastText').textContent = message;
  $('#toastIcon').textContent = isError ? '!' : '✓';
  node.classList.toggle('error', isError);
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 3200);
}

load().then(() => {
  if (state.data) hideLogin();
}).catch(() => {});
setInterval(() => { if (state.authenticated && document.visibilityState === 'visible') load(); }, 60_000);
