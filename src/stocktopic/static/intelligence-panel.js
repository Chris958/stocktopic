(() => {
  const DASHBOARD_PATH = '/api/v1/dashboard';
  let refreshTimer = null;
  let cached = null;
  let cachedAt = 0;

  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[character]));

  const num = (value, digits = 0) => {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(digits) : '—';
  };

  const pct = (value, digits = 1, available = true) => {
    if (!available) return '—';
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
  };

  const metric = (label, value, note = '') => `<div class="intel-metric">
    <span>${escapeHtml(label)}</span>
    <strong>${escapeHtml(value)}</strong>
    ${note ? `<small>${escapeHtml(note)}</small>` : ''}
  </div>`;

  async function dashboard() {
    const now = Date.now();
    if (cached && now - cachedAt < 3000) return cached;
    const response = await fetch(DASHBOARD_PATH, {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }
    });
    if (!response.ok) throw new Error(`dashboard ${response.status}`);
    cached = await response.json();
    cachedAt = now;
    return cached;
  }

  function themeTitle(theme) {
    return theme.final_name || theme.suggested_name || theme.provisional_name || '';
  }

  function findTheme(card, themes) {
    const titleNode = card.querySelector('.theme-title');
    const tagNode = card.querySelector('.theme-tag');
    if (!titleNode) return null;
    const title = titleNode.textContent.replace(/^置顶\s*/, '').trim();
    const tag = tagNode?.textContent.trim() || '';
    return themes.find(theme => themeTitle(theme) === title && (!tag || theme.shared_tag === tag))
      || themes.find(theme => themeTitle(theme) === title)
      || null;
  }

  function stockValue(item) {
    if (!item) return '—';
    const name = item.name || item.code || '—';
    const height = Number(item.board_height || 0);
    return `${name}${height >= 2 ? ` · ${height}板` : ''}`;
  }

  function coreSection(core) {
    const followers = (core.elastic_followers || []).map(item => item.name || item.code).filter(Boolean);
    return `<section class="intel-block">
      <div class="intel-block-head"><strong>核心股结构</strong><small>不是单一龙头排名</small></div>
      <div class="intel-core-grid">
        ${metric('先锋股', stockValue(core.pioneer), core.pioneer ? `先锋 ${num(core.pioneer.pioneer_score)}` : '')}
        ${metric('空间龙', stockValue(core.space_leader), core.space_leader ? `空间 ${num(core.space_leader.space_score)}` : '')}
        ${metric('容量核心', stockValue(core.capacity_core), core.capacity_core ? `容量 ${num(core.capacity_core.capacity_score)}` : '')}
        ${metric('影响力核心', stockValue(core.influence_leader), core.influence_leader ? `影响力 ${num(core.influence_leader.influence_score)}` : '')}
      </div>
      ${followers.length ? `<p class="intel-followers"><span>弹性跟随</span>${followers.map(escapeHtml).join(' · ')}</p>` : ''}
    </section>`;
  }

  function catalystSection(catalyst) {
    return `<section class="intel-block">
      <div class="intel-block-head"><strong>催化质量</strong><b>${num(catalyst.score)}</b></div>
      <div class="intel-metric-grid compact">
        ${metric('真实性', num(catalyst.truth))}
        ${metric('新颖性', num(catalyst.novelty))}
        ${metric('影响力', num(catalyst.impact))}
        ${metric('持续性', num(catalyst.duration))}
      </div>
    </section>`;
  }

  function counterSection(counter) {
    const items = Array.isArray(counter.items) ? counter.items : [];
    return `<section class="intel-block counter-block ${Number(counter.score || 0) >= 60 ? 'high-risk' : ''}">
      <div class="intel-block-head"><strong>反证系统</strong><b>${num(counter.score)}</b></div>
      ${items.length
        ? `<ul class="intel-counter-list">${items.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`
        : '<p class="intel-clear">当前未发现显著反证。</p>'}
    </section>`;
  }

  function marketSection(market) {
    const hasYesterday = Number(market.yesterday_limit_sample_count || 0) > 0;
    const hasPromotion = Number(market.promotion_base_count || 0) > 0;
    const hasBreak = Number(market.break_board_sample_count || 0) > 0;
    return `<section class="intel-block market-block">
      <div class="intel-block-head"><strong>市场环境 · ${escapeHtml(market.label || '—')}</strong><b>${num(market.score)}</b></div>
      <div class="intel-market-grid">
        ${metric('涨停 / 跌停', `${num(market.limit_up_count)} / ${num(market.limit_down_count)}`)}
        ${metric('封板率', pct(market.seal_rate, 1))}
        ${metric('炸板率', pct(market.failed_rate, 1))}
        ${metric('连板高度', `${num(market.max_board_height)}板`)}
        ${metric('真实晋级率', hasPromotion ? pct(market.promotion_rate, 1) : '—', hasPromotion ? `${num(market.promoted_count)}/${num(market.promotion_base_count)}` : '无昨日样本')}
        ${metric('昨日涨停收益', pct(market.yesterday_limit_return, 2, hasYesterday), hasYesterday ? `${num(market.yesterday_limit_sample_count)}只样本` : '无昨日样本')}
        ${metric('打板盈亏', pct(market.board_trade_return, 2, hasYesterday), hasYesterday ? `盈利率 ${pct(market.board_trade_win_rate, 1)}` : '无昨日样本')}
        ${metric('天地板', `${num(market.earth_sky_count)}只`)}
        ${metric('核按钮', `${num(market.nuclear_button_count)}只`, hasPromotion || hasYesterday ? `占昨日板 ${pct(market.nuclear_button_ratio, 1)}` : '')}
        ${metric('断板亏损', pct(market.break_board_return, 2, hasBreak), hasBreak ? `${num(market.break_board_count)}只断板` : '无断板样本')}
      </div>
      <p class="intel-source-note">${escapeHtml(market.version || 'market-environment')} · 前一交易日 ${escapeHtml(market.previous_trade_date || '—')}</p>
    </section>`;
  }

  function buildPanel(theme) {
    const score = theme.score;
    const details = score?.details || {};
    if (!score || !Object.keys(details).length) return '';
    const core = details.core_stock_structure || {};
    const catalyst = details.catalyst_quality || {};
    const counter = details.counter_evidence || {};
    const market = details.market_environment || {};
    const storageKey = `stocktopic-intel-${theme.id}`;
    const open = localStorage.getItem(storageKey) === '1';
    return `<details class="intelligence-disclosure" data-intelligence-id="${theme.id}" ${open ? 'open' : ''}>
      <summary>
        <span><strong>题材结构</strong><small>共振 · 核心股 · 催化 · 反证 · 市场环境</small></span>
        <span class="intel-summary-right">
          <b>共振 ${num(details.resonance_score)}</b>
          <i aria-hidden="true">⌄</i>
        </span>
      </summary>
      <div class="intelligence-content">
        <section class="intel-block">
          <div class="intel-block-head"><strong>题材共振与广度</strong><small>${escapeHtml(score.lifecycle || '—')} · Day ${num(details.day_number)}</small></div>
          <div class="intel-metric-grid">
            ${metric('共振度', num(details.resonance_score))}
            ${metric('题材广度', num(details.breadth_score))}
            ${metric('同步度', num(details.synchronization_score))}
            ${metric('中位涨幅', pct(details.median_pct, 2))}
          </div>
        </section>
        ${coreSection(core)}
        ${catalystSection(catalyst)}
        ${counterSection(counter)}
        ${marketSection(market)}
      </div>
    </details>`;
  }

  async function enhance() {
    let data;
    try {
      data = await dashboard();
    } catch (_) {
      return;
    }
    const themes = Array.isArray(data.themes) ? data.themes : [];
    document.querySelectorAll('.theme-card').forEach(card => {
      if (card.dataset.intelligenceInjected === '1') return;
      const theme = findTheme(card, themes);
      if (!theme?.score) return;
      const html = buildPanel(theme);
      if (!html) return;
      const footnote = card.querySelector('.theme-footnote');
      if (footnote) footnote.insertAdjacentHTML('beforebegin', html);
      else card.insertAdjacentHTML('beforeend', html);
      card.dataset.intelligenceInjected = '1';
      const details = card.querySelector(`details[data-intelligence-id="${theme.id}"]`);
      details?.addEventListener('toggle', () => {
        localStorage.setItem(`stocktopic-intel-${theme.id}`, details.open ? '1' : '0');
      });
    });
  }

  function scheduleEnhance() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      cached = null;
      enhance();
    }, 120);
  }

  const observer = new MutationObserver(mutations => {
    if (mutations.some(item => item.addedNodes.length || item.removedNodes.length)) scheduleEnhance();
  });

  const start = () => {
    ['candidateList', 'confirmedList'].forEach(id => {
      const node = document.getElementById(id);
      if (node) observer.observe(node, { childList: true, subtree: true });
    });
    scheduleEnhance();
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
