import { initApplicationRecords } from './radar-application-records.js';

export function initRadarPersonal({ api, session, host, makeCard, toast, onApplicationChange = () => {}, onManualRead = () => {} }) {
  let saved = new Map();
  let owner = null;
  let timer = null;
  let pending = false;
  let notice = null;
  let appliedPage = 1;
  let appliedTotal = 0;
  let appliedItems = [];
  let appliedLoading = false;
  let appliedError = '';
  let appliedRequest = 0;
  const appliedPageSize = 50;
  const changingApplications = new Set();
  const panel = document.createElement('section');
  panel.dataset.radarPanel = 'saved';
  panel.className = 'radar-tab-panel hidden';
  host.querySelector('[data-radar-panel="jobs"]').after(panel);
  const appliedPanel = document.createElement('section');
  appliedPanel.dataset.radarPanel = 'applied';
  appliedPanel.className = 'radar-tab-panel hidden';
  panel.after(appliedPanel);
  const applicationRecords = initApplicationRecords({ api, session, toast });

  function node(tag, text, className = '') {
    const result = document.createElement(tag);
    result.textContent = text;
    result.className = className;
    return result;
  }
  function updateButtons() {
    document.querySelectorAll('[data-save-job]').forEach(button => {
      const active = saved.has(button.dataset.saveJob);
      button.textContent = active ? '★ 已收藏 · 取消收藏' : '☆ 收藏到待报清单';
      button.setAttribute('aria-pressed', String(active));
    });
  }
  function renderSaved() {
    panel.replaceChildren(node('h3', '待报收藏'), node('p', '数字越小越先报名。收藏不代表已报名；标记已投递后移至“已报名”。收藏和顺序随账号保存。'));
    const visibleSaved = [...saved.values()].filter(item => !['skipped', 'applied'].includes(item.job.application_status));
    if (!visibleSaved.length) panel.append(node('p', '还没有待报名的收藏岗位。展开企业岗位后，点击“收藏到待报清单”。'));
    visibleSaved.sort((a, b) => a.priority - b.priority).forEach(item => {
      const card = makeCard(item.job);
      if (item.unavailable) card.prepend(node('p', '该岗位目前不在机会池中，以下为收藏时的记录；申请前请核对原公告。'));
      const label = node('label', '报名顺序 ');
      const input = document.createElement('input');
      input.type = 'number'; input.min = '1'; input.max = '10000'; input.step = '1';
      input.value = String(item.priority);
      input.setAttribute('aria-label', `${item.job.company} ${item.job.title} 报名顺序`);
      const button = node('button', '保存顺序'); button.type = 'button';
      button.addEventListener('click', async () => {
        if (!input.reportValidity() || !input.value) return;
        const token = session(); button.disabled = true;
        try {
          await api(`/future-radar/saved-jobs/${encodeURIComponent(item.job.id)}`, { method: 'PATCH', body: JSON.stringify({ priority: Number(input.value) }) });
          if (token !== session()) return;
          item.priority = Number(input.value); renderSaved(); toast('报名顺序已保存');
        } catch (_) { toast('报名顺序保存失败，请重试'); }
        finally { button.disabled = false; }
      });
      label.append(input, button); card.prepend(label); panel.append(card);
    });
  }
  function renderApplied() {
    appliedPanel.replaceChildren(node('h3', '已报名'), applicationRecords.panel, node('h4', '机会池中的已投递岗位'), node('p', '显示你已确认投递的岗位，无需先收藏；截止或关闭后仍保留报名记录。这里不受机会池筛选和精选范围限制。'));
    const refreshButton = node('button', '刷新已报名'); refreshButton.type = 'button';
    refreshButton.disabled = appliedLoading;
    refreshButton.addEventListener('click', () => Promise.allSettled([applicationRecords.load(1), loadApplied(appliedPage)]));
    appliedPanel.append(refreshButton);
    if (appliedLoading) {
      const loading = node('p', '正在读取关联的具体岗位…'); loading.setAttribute('role', 'status');
      appliedPanel.append(loading);
    }
    if (appliedError) {
      const error = node('p', appliedError); error.setAttribute('role', 'alert'); appliedPanel.append(error);
    }
    if (!appliedLoading && !appliedError && !appliedTotal) appliedPanel.append(node('p', '尚未关联机会池中的具体岗位。上方历史报名记录不受影响；在岗位卡片中选择“已投递”后，会显示在这里。'));
    const list = node('div', '', 'recruitment-jobs');
    appliedItems.forEach(job => {
      const card = makeCard(job);
      if (job.status === 'closed') card.prepend(node('p', '招聘已关闭 · 已报名记录保留'));
      list.append(card);
    });
    appliedPanel.append(list);
    const pages = Math.max(1, Math.ceil(appliedTotal / appliedPageSize));
    const navigation = node('nav', '', 'radar-pagination'); navigation.setAttribute('aria-label', '已报名分页');
    const previous = node('button', '← 上一页'); previous.type = 'button'; previous.disabled = appliedLoading || appliedPage <= 1;
    previous.addEventListener('click', () => loadApplied(appliedPage - 1));
    const next = node('button', '下一页 →'); next.type = 'button'; next.disabled = appliedLoading || appliedPage >= pages;
    next.addEventListener('click', () => loadApplied(appliedPage + 1));
    navigation.append(previous, node('span', `第 ${appliedPage} / ${pages} 页 · 共 ${appliedTotal} 条关联岗位`), next);
    appliedPanel.append(navigation);
  }
  async function loadApplied(page = 1) {
    const token = session();
    if (!token) return;
    onManualRead();
    const request = ++appliedRequest;
    const requestedPage = Math.max(1, page);
    appliedLoading = true; appliedError = ''; renderApplied();
    // Personal history deliberately has its own query. Pool filters, balanced
    // limits and active-only dates must never hide an existing application.
    const query = new URLSearchParams({ status: 'all', application_status: 'applied', view: 'jobs',
      compact: 'true', balanced_only: 'false', priority_only: 'false', sort: 'changed',
      page: String(requestedPage), page_size: String(appliedPageSize) });
    try {
      const payload = await api(`/future-radar/opportunities?${query}`, { timeoutMs: 180000 });
      if (token !== session() || request !== appliedRequest) return;
      const total = Number(payload.total) || 0;
      const lastPage = Math.max(1, Math.ceil(total / appliedPageSize));
      if (requestedPage > lastPage) return loadApplied(lastPage);
      appliedPage = requestedPage; appliedTotal = total; appliedItems = payload.items || [];
    } catch (_) {
      if (token !== session() || request !== appliedRequest) return;
      appliedError = '关联岗位暂时读取失败，上方历史报名记录不受影响。可点击“刷新已报名”重试。';
    } finally {
      if (token === session() && request === appliedRequest) { appliedLoading = false; renderApplied(); }
    }
  }
  function saveButton(job) {
    const button = node('button', saved.has(job.id) ? '★ 已收藏 · 取消收藏' : '☆ 收藏到待报清单', 'job-watch-button');
    button.type = 'button'; button.dataset.saveJob = job.id;
    button.setAttribute('aria-pressed', String(saved.has(job.id)));
    button.addEventListener('click', async () => {
      const token = session(); const exists = saved.has(job.id); button.disabled = true;
      const priority = Math.min(10000, Math.max(0, ...[...saved.values()].map(item => item.priority)) + 1);
      try {
        await api(`/future-radar/saved-jobs/${encodeURIComponent(job.id)}`, {
          method: exists ? 'DELETE' : 'PUT', ...(!exists ? { body: JSON.stringify({ priority }) } : {}),
        });
        if (token !== session()) return;
        if (exists) saved.delete(job.id); else saved.set(job.id, { job, priority });
        updateButtons(); renderSaved(); toast(exists ? '已取消收藏' : '收藏已保存');
      } catch (_) { toast('收藏保存失败，请重试'); }
      finally { button.disabled = false; }
    });
    return button;
  }
  function applicationControl(job) {
    const control = node('div', '', 'radar-application-control');
    control.dataset.applicationJob = job.id;
    control.dataset.applicationStatus = job.application_status || 'not_applied';
    const badge = node('span', '', 'radar-application-badge');
    badge.setAttribute('aria-live', 'polite');
    const select = document.createElement('select');
    select.setAttribute('aria-label', `${job.company || ''} ${job.title || ''} 投递状态`);
    const labels = { not_applied: '未投递', planned: '准备投递', applied: '已投递', skipped: '跳过这个岗位' };
    for (const [value, text] of Object.entries(labels)) {
      const option = node('option', text); option.value = value; select.append(option);
    }
    const paint = () => {
      const status = control.dataset.applicationStatus;
      badge.textContent = labels[status] || labels.not_applied;
      select.value = status;
      select.disabled = changingApplications.has(job.id);
    };
    control.addEventListener('applicationchange', paint);
    const updateControls = status => {
      document.querySelectorAll('[data-application-job]').forEach(item => {
        if (item.dataset.applicationJob !== job.id) return;
        if (status) item.dataset.applicationStatus = status;
        item.dispatchEvent(new Event('applicationchange'));
      });
    };
    select.addEventListener('change', async () => {
      if (changingApplications.has(job.id)) return;
      const token = session();
      const status = select.value;
      changingApplications.add(job.id); updateControls();
      try {
        const result = await api(`/future-radar/opportunities/${encodeURIComponent(job.id)}/application`, {
          method: 'PUT', body: JSON.stringify({ status }), timeoutMs: 180000,
        });
        if (token !== session()) return;
        job.application_status = result.application_status;
        if (saved.has(job.id)) saved.get(job.id).job.application_status = result.application_status;
        updateControls(result.application_status);
        renderSaved();
        if (!appliedPanel.classList.contains('hidden')) loadApplied(appliedPage);
        toast(`已保存：${labels[result.application_status]}`);
        onApplicationChange(job.id, result.application_status);
      } catch (_) { if (token === session()) toast('投递状态保存失败，请重试'); }
      finally {
        if (token === session()) { changingApplications.delete(job.id); updateControls(); }
      }
    });
    control.append(badge, select); paint();
    return control;
  }
  function showNotice(payload) {
    if (notice || !host.open || !payload.items.length) return;
    let reviewed = false;
    const dialog = document.createElement('dialog');
    dialog.className = 'radar-update-dialog';
    dialog.setAttribute('aria-label', '新增岗位提醒');
    dialog.append(node('h2', `发现 ${payload.items.length} 个新增岗位`), node('p', '请点击查看，核对岗位后确认关闭。未确认的提醒会保留。'));
    const body = node('div', '', 'radar-update-list'); body.hidden = true;
    const view = node('button', '查看新增岗位'); view.type = 'button';
    const close = node('button', '已查看，关闭提醒'); close.type = 'button'; close.hidden = true;
    const error = node('p', ''); error.setAttribute('role', 'alert');
    view.addEventListener('click', () => {
      body.replaceChildren(...payload.items.map(item => makeCard(item.job)));
      body.hidden = false; reviewed = true; view.hidden = true; close.hidden = false;
    });
    close.addEventListener('click', async () => {
      const token = session(); close.disabled = true;
      try {
        await api('/future-radar/notifications/ack', { method: 'POST', body: JSON.stringify({ through_event_id: payload.through_event_id }) });
        if (token !== session()) return;
        dialog.close(); dialog.remove(); notice = null; refresh();
      } catch (_) { error.textContent = '确认未保存，请重试；提醒仍然保留。'; }
      finally { close.disabled = false; }
    });
    dialog.addEventListener('cancel', event => { event.preventDefault(); if (reviewed) close.click(); });
    dialog.append(view, body, close, error); document.body.append(dialog); notice = dialog; dialog.showModal();
  }
  async function refresh() {
    const token = session();
    if (!token || !host.open || document.hidden || pending) return;
    if (owner !== token) { saved = new Map(); owner = token; }
    pending = true;
    try {
      const [bookmarks, notifications] = await Promise.allSettled([
        api('/future-radar/saved-jobs'), api('/future-radar/notifications'),
      ]);
      if (token !== session()) return;
      if (bookmarks.status === 'fulfilled') {
        saved = new Map(bookmarks.value.items.map(item => [item.job.id, item]));
        updateButtons();
        // Do not overwrite a priority value while the user is editing it.
        if (!panel.contains(document.activeElement)) renderSaved();
      }
      if (notifications.status === 'fulfilled') showNotice(notifications.value);
    } finally { pending = false; }
  }
  function start() { refresh(); if (!timer) timer = setInterval(refresh, 30000); }
  function stop() { clearInterval(timer); timer = null; }
  function reset() {
    stop(); owner = null; saved.clear(); changingApplications.clear();
    ++appliedRequest; appliedPage = 1; appliedTotal = 0; appliedItems = []; appliedLoading = false; appliedError = '';
    applicationRecords.reset();
    notice?.close(); notice?.remove(); notice = null; panel.replaceChildren(); appliedPanel.replaceChildren();
  }
  host.addEventListener('close', stop);
  return { saveButton, applicationControl, renderSaved, loadApplied,
    showApplied: () => Promise.allSettled([applicationRecords.load(1), loadApplied(1)]), start, reset };
}
