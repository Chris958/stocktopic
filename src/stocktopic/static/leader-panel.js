(() => {
  const DEFAULT_LIMIT = 6;
  let snapshot = null;
  let expanded = false;
  let poolRequest = null;
  let historyRequest = null;
  let historyCursor = null;
  let accountVersion = 0;
  let readFailed = false;
  let lastMarkup = '';

  const list = $('#leaderList');
  const notice = $('#leaderStatus');
  const expand = $('#leaderExpand');
  const logPanel = $('#leaderHistory');
  const logList = $('#leaderHistoryList');
  const logNotice = $('#leaderHistoryStatus');
  const more = $('#leaderHistoryMore');
  const text = escapeHtml;

  function render() {
    const failed = readFailed;
    if (!snapshot) {
      notice.textContent = failed ? '读取失败，刷新后重试' : '等待首批有效盘中行情';
      return;
    }
    const items = snapshot.items || [];
    const opportunities = failed ? 0 : items.filter(s => s.action === 'opportunity').length;
    const updateState = failed ? '更新失败，窗口待确认'
      : snapshot.state === 'waiting_for_data' ? '行情待补'
      : snapshot.state === 'closed' ? '非连续交易时段'
      : snapshot.state === 'awaiting' ? '等待首批行情' : '';
    notice.textContent = [
      `跟踪 ${items.length} 只 · 机会 ${opportunities} 只`,
      snapshot.observed_at ? formatDataTime(snapshot.observed_at) : '',
      updateState,
    ].filter(Boolean).join(' · ');
    const visible = expanded ? items : items.slice(0, DEFAULT_LIMIT);
    const markup = visible.length ? visible.map(item => card(item, failed)).join('')
      : emptyState('暂无候选', '符合强势与题材共振条件的股票会自动进入。');
    if (lastMarkup !== markup) { list.innerHTML = markup; lastMarkup = markup; }
    expand.hidden = items.length <= DEFAULT_LIMIT;
    expand.setAttribute('aria-expanded', String(expanded));
    expand.textContent = expanded ? '收起候选' : `查看全部 ${items.length} 只`;
  }

  function card(item, failed) {
    const primary = item.directions[0];
    const window = failed ? null : item.window;
    const feedback = primary.feedback;
    const quote = item.quote;
    const scopeLabels = { formal: '正式题材', local: '本地观察', archived: '归档跟踪' };
    const status = failed && item.action === 'opportunity' ? '数据更新待确认' : item.status;
    const reason = failed && item.action === 'opportunity'
      ? '等待下一次刷新确认窗口是否仍有效。' : item.reason;
    return `<article class="leader-card">
      <div class="leader-card-head">
        <div><h4>${text(item.name)}</h4><span>${text(item.code)} · ${primary.role === 'height' ? '高标参考' : '承接候选'}
          ${primary.height ? ` · 近期 ${text(primary.height)} 连板` : ''}</span></div>
        <strong class="leader-action ${window ? 'leader-opportunity' : ''}">${window ? '关注机会' : '等'}</strong>
      </div>
      <p class="leader-directions">${item.directions.map(d =>
        `<span>${text(d.name)} · ${text(scopeLabels[d.scope])}</span>`).join('')}</p>
      <dl class="leader-evidence">
        <div><dt>入池原因</dt><dd>${text(item.entry_reason)}</dd></div>
        <div><dt>当前情况</dt><dd><strong>${text(status)}</strong> · ${text(reason)}</dd></div>
        ${window ? `<div><dt>观察窗口</dt><dd>${text(formatTime(window.triggered_at))} 起，最晚 ${text(formatTime(window.valid_until))}
          · 跟随股 ${text(window.follower_name || window.follower)}
          · 跌回 ${text(Number(window.support_price).toFixed(2))} 或同题材股票转弱即失效。</dd></div>` : ''}
      </dl>
      <p class="leader-meta">入池 ${text(formatDataTime(item.entered_at))}
        ${quote ? ` · 个股 ${text(formatPct(quote.pct))}` : ''}
        ${primary.peer_pct != null ? ` · 对照股中位 ${text(formatPct(primary.peer_pct))}` : ''}
        ${feedback && feedback.expected ? ` · 昨强股样本 ${text(feedback.observed)}/${text(feedback.expected)}
          · 大幅走弱 ${text(feedback.severe)} 只` : ''}</p>
    </article>`;
  }

  window.refreshLeaderPanel = function () {
    if (poolRequest || !state.authenticated) return;
    const version = accountVersion;
    const previousHead = snapshot?.history_head;
    const request = api('/api/v1/leader-pool').then(value => {
      if (version !== accountVersion) return;
      snapshot = value;
      readFailed = false;
      render();
      if (logPanel.open && previousHead !== value.history_head) {
        void loadHistory();
      }
    }).catch(() => {
      if (version === accountVersion) { readFailed = true; render(); }
    }).finally(() => {
      if (poolRequest === request) poolRequest = null;
    });
    poolRequest = request;
  };

  async function loadHistory(before = null) {
    if (historyRequest || !state.authenticated) return;
    const version = accountVersion;
    const path = '/api/v1/leader-pool/history' + (before ? `?before=${encodeURIComponent(before)}` : '');
    logNotice.textContent = '正在读取记录…';
    more.disabled = true;
    const request = api(path);
    historyRequest = request;
    try {
      const result = await request;
      if (version !== accountVersion) return;
      if (!before) logList.replaceChildren();
      logList.insertAdjacentHTML('beforeend', result.items.map(entry => `<li>
        <strong>${entry.event === 'entered' ? '入池' : '移出'} · ${text(entry.name)}</strong>
        <span>${text(entry.code)} · ${text(entry.directions.join('、'))} · ${text(formatDataTime(entry.at))}</span>
        <p>${text(entry.reason)}</p>
      </li>`).join(''));
      logNotice.textContent = logList.children.length ? '' : '暂无入池或移出记录。';
      historyCursor = result.next_cursor;
      more.hidden = !historyCursor;
      more.textContent = '加载更早记录';
    } catch (_) {
      if (version !== accountVersion) return;
      logNotice.textContent = '读取失败，请重试。';
      historyCursor = before;
      more.hidden = false;
      more.textContent = '重试';
    } finally {
      if (historyRequest === request) {
        historyRequest = null;
        more.disabled = false;
      }
    }
  }

  window.clearLeaderPanel = () => {
    accountVersion += 1;
    snapshot = poolRequest = historyRequest = historyCursor = null;
    expanded = readFailed = false;
    lastMarkup = '';
    list.replaceChildren();
    logList.replaceChildren();
    logPanel.open = false;
    logNotice.textContent = '';
    notice.textContent = '等待行情';
    expand.hidden = more.hidden = true;
    more.disabled = false;
  };
  expand.addEventListener('click', () => { expanded = !expanded; render(); });
  logPanel.addEventListener('toggle', () => { if (logPanel.open) void loadHistory(); });
  more.addEventListener('click', () => void loadHistory(historyCursor));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') window.refreshLeaderPanel();
  });
  if (state.authenticated) window.refreshLeaderPanel();
})();
