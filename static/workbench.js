/* Meeting research workbench. All meeting and source content stays plain text. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  const themeValues = new Set(['system', 'light', 'dark']);
  let themePreference = window.__ZSJH_THEME_PREFERENCE__;
  if (!themeValues.has(themePreference)) {
    try { themePreference = localStorage.getItem('wb_theme'); } catch { /* Browser storage may be unavailable. */ }
  }
  if (!themeValues.has(themePreference)) themePreference = 'system';
  function applyTheme(value = themePreference) {
    themePreference = themeValues.has(value) ? value : 'system';
    document.documentElement.dataset.theme = themePreference === 'system' ? (systemTheme.matches ? 'dark' : 'light') : themePreference;
    $('theme-preference').value = themePreference;
  }
  applyTheme();
  systemTheme.addEventListener('change', () => applyTheme());
  window.addEventListener('storage', event => { if (event.key === 'wb_theme' && !nativeAvailable()) applyTheme(event.newValue); });
  const state = {
    token: '', home: null, snapshot: null, meetingId: '', selectedQuestionId: '',
    request: null, selection: 0, detailTab: 'answer', dialogQuestion: null, queryQuestion: null,
    artifactCache: new Map(), artifactRequests: new Map(), detailSignature: '',
    transcriptSignature: '', questionSignature: '', activitySignature: '', noticeTimer: null,
    pendingButtons: new Set(), route: 'meetings', filter: 'all', queueFilter: 'all', descending: true,
    search: '', librarySignature: '', recentIds: [], homeRequest: null,
    native: window.__ZSJH_NATIVE__ || null, switchTarget: null, endTarget: '', importTarget: '', importFileID: '', nativeFileError: '', permissionTarget: '',
    serviceSettings: null, settingsRequest: null, settingsBusy: false, settingsDirty: false,
    clearModelKey: false, clearSearchKey: false, manageTarget: null, deletionReady: false,
    snapshotReceivedAt: 0, serverOffset: 0, transcriptHistory: null, returnNote: null,
  };
  const questionStates = {
    candidate: '待确认', queued: '等待研究', running: '正在研究', completed: '任务完成',
    insufficient: '研究结束·证据不足', failed: '任务失败', cancelled: '已取消',
    superseded: '已被新版本替代', outcome_unknown: '请求结果未知', paused: '已暂停',
    waiting_dependency: '等待前置资料',
    needs_review: '依据已更正·待复核', backlog: '队列已满·待整理',
  };
  const evidenceStates = {supported: '有来源支持', mixed: '证据存在分歧', insufficient: '证据不足', model_explanation: '模型解释·未核验'};
  const stages = {searching: '搜索资料', reading: '读取原文', synthesizing: '整理回答', publishing: '保存文档', queued: '等待执行', detecting: '问题识别', research_unavailable: '研究服务暂不可用', budget_exhausted: '本次请求额度已用完', invalid_evidence_or_contract: '返回资料未通过校验', outcome_unknown: '请求结果未知，请确认后重试'};
  const captureStates = {idle: '未连接', disconnected: '未连接', ready: '音频已就绪', listening: '正在收听', running: '正在采音', capturing: '正在采音', paused: '收听已暂停', stopped: '采音已停止', error: '音频异常', gap: '采音存在缺口', level: '收到音频电平', status: '组件状态更新', unavailable: '音频不可用', bridge: '知了桥接', not_connected: '未连接'};
  const meetingStates = {active: '会议进行中', running: '会议进行中', open: '会议进行中', paused: '会议已暂停', ended: '会议已结束', stopped: '已全部停止', expired: '数据已到期清理', created: '会议已创建', deleting: '正在删除', delete_failed: '删除未完成'};
  const sources = {system: '电脑声音', microphone: '麦克风', mic: '麦克风', file: '音频文件', manual: '文字补充', synthetic: '合成讨论', cicada: '知了转写'};
  const audioInputs = {
    system: {label: '电脑声音', sources: ['system'], help: '仅收听电脑上播放的声音。'},
    microphone: {label: '麦克风', sources: ['microphone'], help: '直接对着系统当前输入设备讲话，声音会在本机转写。'},
    both: {label: '电脑声音＋麦克风', sources: ['system', 'microphone'], help: '同时收听会议播放和你的发言。建议戴耳机，避免外放声音被重复收录。'},
  };
  const activeStates = new Set(['candidate', 'queued', 'running', 'paused', 'waiting_dependency', 'needs_review', 'backlog']);
  const retryStates = new Set(['failed', 'insufficient', 'cancelled', 'outcome_unknown', 'paused']);

  function text(node, value) {
    const next = value === undefined || value === null ? '' : String(value);
    if (node.textContent !== next) node.textContent = next;
  }
  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) text(node, value);
    return node;
  }
  function icon(name, className = '') {
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    if (className) node.setAttribute('class', className);
    node.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    node.append(use);
    return node;
  }
  function empty(container, value) { container.replaceChildren(element('p', 'empty-state', value)); }
  function readable(value) {
    if (typeof value === 'string') return value;
    if (value && typeof value === 'object') return value.text || value.message || value.reason || value.title || JSON.stringify(value);
    return value === null || value === undefined ? '' : String(value);
  }
  function date(value) {
    if (value === undefined || value === null || value === '') return null;
    const result = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value);
    return Number.isNaN(result.getTime()) ? null : result;
  }
  function retracted(segment) { return segment.type === 'transcript.retracted'; }
  function segmentSource(segment) { return state.snapshot?.meeting?.mode === 'demo' ? '示例发言' : segment.capture_epoch === 'manual' ? '文字补充' : sources[segment.source] || segment.source || '转写'; }
  function audioTime(ms = 0) { const seconds = Math.floor(ms / 1000); return `${Math.floor(seconds / 3600) ? Math.floor(seconds / 3600) + ':' : ''}${String(Math.floor(seconds / 60) % 60).padStart(2,'0')}:${String(seconds % 60).padStart(2,'0')}`; }
  function segmentTime(segment) { return segment.received_at || segment.occurred_at; }
  function time(value, full = false) {
    const result = date(value);
    return result ? result.toLocaleString('zh-CN', full ? {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false} : {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}) : '时间未知';
  }
  function safeURL(value) {
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url : null;
    } catch { return null; }
  }
  function sourceLink(source, label) {
    const url = safeURL(source.url);
    if (!url) return element('span', '', label);
    const link = element('a', '', label);
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  }
  function notice(message, error = false) {
    clearTimeout(state.noticeTimer);
    text($('notice'), message);
    $('notice').dataset.error = String(error);
    $('notice').hidden = false;
    state.noticeTimer = setTimeout(() => { $('notice').hidden = true; }, error ? 12000 : 5000);
  }
  function nativeAvailable() { return Boolean(window.webkit?.messageHandlers?.workbench); }
  function nativePost(action, meetingId, fileId, audioSource, themePreference) {
    if (!nativeAvailable()) { notice('请在真实交互桌面 App 中使用本机功能。', true); return false; }
    window.webkit.messageHandlers.workbench.postMessage({action, ...(meetingId ? {meetingId} : {}), ...(fileId ? {fileId} : {}), ...(audioSource ? {audioSource} : {}), ...(themePreference ? {themePreference} : {})});
    return true;
  }
  async function stopCaptureAndWait() {
    const bound = state.native?.meetingId;
    if (!bound) return;
    await new Promise((resolve, reject) => {
      const cleanup = () => { window.removeEventListener('zsjh-native-state', changed); clearTimeout(timeout); };
      const changed = (event) => { if (!event.detail?.meetingId) { cleanup(); resolve(); } };
      const timeout = setTimeout(() => { cleanup(); reject(new Error('音频仍在收尾，请稍后重试。会议尚未结束。')); }, 14000);
      window.addEventListener('zsjh-native-state', changed);
      if (state.native?.canStop) nativePost('stopCapture', bound);
      else nativePost('requestState');
    });
  }
  function calendarDate(value) {
    return date(value)?.toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false}) || '—';
  }
  function meetingHash() {
    const detail = $('work-grid').dataset.mobilePanel === 'results' && state.selectedQuestionId;
    const left = $('work-grid').dataset.leftPane;
    return `#meeting/${encodeURIComponent(state.meetingId)}${detail ? `?question=${encodeURIComponent(state.selectedQuestionId)}&tab=${state.detailTab}` : ['files','summary'].includes(left) ? `?view=${left}` : ''}`;
  }
  function setRoute(route, updateHistory = true) {
    state.route = ['meetings', 'materials', 'meeting', 'guide', 'settings'].includes(route) ? route : 'meetings';
    $('workspace').dataset.route = state.route;
    $('workspace').dataset.mobileNav = 'closed';
    $('toggle-navigation').setAttribute('aria-expanded', 'false');
    for (const name of ['library', 'meeting', 'guide', 'settings']) $(name + '-view').hidden = name === 'library' ? !['meetings', 'materials'].includes(state.route) : name !== state.route;
    for (const button of document.querySelectorAll('button[data-route]')) {
      if (button.dataset.route === state.route) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
    $('back-library').hidden = state.route !== 'meeting';
    text($('breadcrumb'), state.route === 'meeting' ? state.snapshot?.meeting?.title || '会议' : {meetings:'工作区', materials:'工作区', guide:'如何使用', settings:'设置'}[state.route]);
    $('library-search').placeholder = state.route === 'materials' ? '搜索资料' : '搜索会议';
    if (updateHistory) {
      const hash = state.route === 'meeting' ? meetingHash() : `#${state.route}`;
      if (location.hash !== hash) history.pushState(null, '', hash);
    }
    renderLibrary();
    renderSettings();
    if (state.route === 'settings') loadServiceSettings();
    $('main').scrollTop = 0;
  }
  async function readRoute() {
    const [path, query = ''] = location.hash.slice(1).split('?');
    if (path.startsWith('meeting/')) {
      let id;
      try { id = decodeURIComponent(path.slice(8)); } catch { setRoute('meetings'); return; }
      const params = new URLSearchParams(query);
      if ((state.home?.meetings || []).some((m) => m.id === id)) {
        const opened = await selectMeeting(id, params.get('question') || '', ['sources','document'].includes(params.get('tab')) ? params.get('tab') : 'answer', false);
        if (opened && ['files','summary'].includes(params.get('view'))) setMeetingTab(params.get('view'), false);
        return;
      }
      notice('这场会议已不可访问或已清理。', true);
    }
    setRoute(path, false);
  }
  function renderRecent() {
    const meetings = state.home?.meetings || [];
    const rows = state.recentIds.map(id => meetings.find(m => m.id === id)).filter(Boolean).slice(0, 4);
    const list = $('recent-meetings');
    const signature = JSON.stringify(rows.map(m => [m.id, m.title]));
    if (list.dataset.signature !== signature) {
      list.dataset.signature = signature;
      list.replaceChildren(...rows.map(meeting => {
        const button = element('button', 'recent-link');
        button.dataset.meetingId = meeting.id;
        button.title = meeting.title;
        button.setAttribute('aria-label', meeting.title);
        button.append(icon('file'), element('span', '', meeting.title));
        return button;
      }));
    }
    document.querySelector('.recent-section').hidden = !rows.length;
    const current = meetings.find(m => m.id === state.native?.meetingId) || (state.snapshot?.meeting?.state === 'active' ? state.snapshot.meeting : null);
    $('current-meeting-nav').hidden = !current;
    $('return-meeting').dataset.meetingId = current?.id || '';
    text($('current-meeting-name'), current?.title || '');
    $('return-meeting').title = current ? `返回会议：${current.title}` : '';
    $('return-meeting').setAttribute('aria-label', $('return-meeting').title);
    for (const button of document.querySelectorAll('.app-sidebar [data-meeting-id]')) {
      if (state.route === 'meeting' && button.dataset.meetingId === state.meetingId) button.setAttribute('aria-current','page');
      else button.removeAttribute('aria-current');
    }
  }
  const materialShelf = {filter:'all', id:'', requestKey:'', request:null};
  const desktopLibrary = matchMedia('(min-width: 981px)');
  const documentLabels = {saved:'已保存', pending:'待保存', save_failed:'保存失败', conflict:'保留人工修改'};
  function previewLibraryMaterial(record, force = false) {
    const pane = $('material-preview');
    const key = record && JSON.stringify([record.id, record.version, record.state, record.basis_stale]);
    pane.hidden = !record;
    if (!force && materialShelf.requestKey === key) return;
    materialShelf.request?.abort();
    materialShelf.request = null; materialShelf.requestKey = key;
    text($('material-preview-body'), '');
    if (!record) { text($('material-preview-title'), ''); text($('material-preview-state'), ''); return; }
    text($('material-preview-title'), record.title);
    text($('material-preview-state'), '正在读取文档…');
    const controller = new AbortController(), token = state.token;
    materialShelf.request = controller;
    const current = () => materialShelf.request === controller && state.token === token && state.route === 'materials';
    api(`/live/v2/artifacts/${encodeURIComponent(record.id)}`, {signal:controller.signal}).then(result => {
      if (!current()) return;
      const file = result.document || record;
      const labels = [`第 ${record.version} 版`, documentLabels[file.state] || file.state || '状态未知'];
      if (file.basis_stale) labels.push('依据已更正，仅供历史参考');
      if (file.externally_modified) labels.push('显示磁盘中的人工修改');
      if (file.on_disk === false && file.state === 'saved') labels.push('磁盘文件不存在，显示登记内容');
      text($('material-preview-state'), labels.join(' · '));
      text($('material-preview-body'), result.content || '文档内容为空。');
    }).catch(error => { if (current() && !controller.signal.aborted) text($('material-preview-state'), `未能读取文档：${error.message}`); });
  }
  function renderLibrary() {
    const home = state.home;
    if (!home) return;
    const materials = state.route === 'materials';
    const canManage = !materials && home.member?.role === 'owner';
    $('meeting-actions-column').hidden = !canManage;
    document.querySelector('.library-column-head').classList.toggle('has-meeting-actions', canManage);
    const meetings = home.meetings || [];
    const query = state.search.trim().toLocaleLowerCase();
    let rows = materials ? home.materials || [] : meetings;
    rows = rows.filter(row => {
      const meeting = materials ? meetings.find(m => m.id === row.meeting_id) : row;
      if (query && !`${row.title} ${materials ? meeting?.title || '' : ''}`.toLocaleLowerCase().includes(query)) return false;
      if (state.filter === 'import' && meeting?.input_kind !== 'audio_file') return false;
      if (state.filter === 'active' && meeting?.state !== 'active') return false;
      if (state.filter === 'ended' && meeting?.state === 'active') return false;
      return !materials || materialShelf.filter === 'all' || (materialShelf.filter === 'saved' && row.state === 'saved') || (materialShelf.filter === 'stale' && row.basis_stale) || (materialShelf.filter === 'trouble' && ['save_failed','conflict'].includes(row.state));
    }).slice().sort((a, b) => ((materials ? a.created_at : a.updated_at || a.created_at) - (materials ? b.created_at : b.updated_at || b.created_at)) * (state.descending ? -1 : 1));
    text($('library-title'), materials ? '全部资料' : '全部会议');
    text($('library-description'), materials ? `已登记 ${home.materials?.length || 0} 份资料` : `${meetings.length} 场会议 · ${meetings.filter(m => m.state === 'active').length} 场进行中`);
    text($('name-column'), materials ? '资料名称' : '会议名称');
    text(document.querySelector('.count-column'), materials ? '版本' : '查证资料');
    text(document.querySelector('.date-column'), materials ? '生成时间' : '最近更新');
    text($('library-sort'), `${materials ? '生成时间' : '最近更新'} ${state.descending ? '↓' : '↑'}`);
    $('welcome-strip').hidden = materials || Boolean(query) || home.project?.data_scope !== 'synthetic';
    $('library-new-meeting').hidden = materials;
    $('library-import-audio').hidden = materials;
    const active = state.route === 'meetings' && !query && ['all','active'].includes(state.filter) && (meetings.find(m => m.state === 'active' && m.id === state.native?.meetingId) || meetings.find(m => m.state === 'active' && m.id === state.meetingId) || meetings.find(m => m.state === 'active'));
    $('library-current').hidden = !active;
    if (active) {
      text($('library-current-title'), active.title || '未命名会议');
      text($('library-current-meta'), [active.input_kind === 'audio_file' ? '音频导入' : active.mode === 'demo' ? '示例会议' : '会议进行中', `${active.question_count ?? 0} 个查证问题`, `${active.document_count ?? 0} 份文件`].join(' · '));
      $('library-resume').dataset.meetingId = active.id;
    }
    $('material-filters').hidden = !materials;
    $('library-columns').classList.toggle('with-preview', materials && rows.length > 0);
    const selected = materials && (rows.find(r => r.id === materialShelf.id) || rows[0]);
    materialShelf.id = selected?.id || '';
    previewLibraryMaterial(desktopLibrary.matches && selected || null);
    const signature = JSON.stringify([state.route, state.filter, materialShelf.filter, materialShelf.id, state.search, state.descending, canManage, rows]);
    if (signature !== state.librarySignature) {
      state.librarySignature = signature;
      const list = $('library-list');
      const focused = list.contains(document.activeElement) ? document.activeElement.dataset.rowId : null;
      list.replaceChildren(...rows.map(row => {
        const meeting = materials ? meetings.find(m => m.id === row.meeting_id) : row;
        const wrap = element('div', canManage ? 'library-entry meeting-entry' : 'library-entry'); wrap.setAttribute('role', 'listitem');
        const button = element('button', 'library-row');
        button.dataset.rowId = row.id;
        button.dataset.meetingId = materials ? row.meeting_id : row.id;
        if (materials) {
          button.dataset.materialQuestion = row.question_id;
          button.setAttribute('aria-current', String(row.id === materialShelf.id));
          button.setAttribute('aria-controls', 'material-preview');
        }
        const name = element('span', 'library-name');
        const paper = element('span', 'paper-icon'); paper.append(icon(materials ? 'file' : 'meetings'));
        const copy = element('span', 'library-name-copy');
        copy.append(element('span', 'row-title', row.title || '未命名'));
        copy.append(element('span', 'row-subtitle', materials ? meeting?.title || '会议资料' : `${row.input_kind === 'audio_file' ? '音频导入' : row.data_scope === 'synthetic' ? '示例会议' : '正式会议'} · ${row.question_count || 0} 个查证问题`));
        name.append(paper, copy);
        const status = element('span', 'row-state');
        status.dataset.state = row.state;
        status.dataset.stale = String(Boolean(row.basis_stale));
        status.append(element('span', 'status-dot'), document.createTextNode(materials ? row.basis_stale ? '待复核' : documentLabels[row.state] || row.state : row.state === 'active' ? '进行中' : meetingStates[row.state] || row.state));
        const updated = materials ? row.created_at : row.updated_at || row.created_at;
        const dateNode = element('time', 'row-date', calendarDate(updated));
        if (date(updated)) dateNode.dateTime = date(updated).toISOString();
        button.append(name, status, element('span', 'row-count', materials ? `v${row.version}` : `${row.document_count || 0} 份`), dateNode);
        wrap.append(button);
        if (canManage) {
          const actions = element('button', 'meeting-row-actions');
          actions.type = 'button'; actions.dataset.manageMeeting = row.id;
          actions.setAttribute('aria-label', `操作会议：${row.title}`);
          actions.setAttribute('aria-haspopup', 'true'); actions.setAttribute('aria-expanded', 'false');
          actions.setAttribute('aria-controls', 'meeting-actions-menu');
          actions.append(icon('more')); wrap.append(actions);
        }
        return wrap;
      }));
      if (focused) [...list.querySelectorAll('button')].find(node => node.dataset.rowId === focused)?.focus({preventScroll:true});
    }
    $('library-empty').hidden = Boolean(rows.length);
    $('empty-create').hidden = Boolean(query) || materials || state.filter !== 'all';
    const filteredMaterials = materials && (materialShelf.filter !== 'all' || state.filter !== 'all');
    text($('library-empty-title'), query ? '没有找到匹配内容' : filteredMaterials ? '暂无符合筛选的资料' : materials ? '资料会在这里汇集' : state.filter === 'all' ? '开始你的第一场会议' : '暂无符合筛选的会议');
    text($('library-empty-message'), query ? '试试更短的关键词，或清除搜索。' : filteredMaterials ? '切换资料状态或会议筛选查看其他资料。' : materials ? '打开一场会议，在“查证问题”中添加问题；生成的 Markdown 会出现在这里。' : state.filter === 'all' ? '首次使用先到“设置”安装中文转写模型、配置联网服务。准备好后开始会议，也可以导入已有录音。' : '切换“全部”查看已有会议。');
    renderRecent();
  }
  function closeMeetingActions() {
    $('meeting-actions-menu').hidePopover();
    document.querySelectorAll('.meeting-row-actions[aria-expanded="true"]').forEach(button => button.setAttribute('aria-expanded', 'false'));
  }
  function showMeetingActions(button) {
    const meeting = state.home?.meetings.find(m => m.id === button.dataset.manageMeeting);
    if (!meeting) return;
    const wasOpen = button.getAttribute('aria-expanded') === 'true';
    closeMeetingActions();
    if (wasOpen) return;
    state.manageTarget = meeting;
    $('rename-meeting-action').disabled = ['expired', 'deleting', 'delete_failed'].includes(meeting.state);
    const rect = button.getBoundingClientRect();
    const menu = $('meeting-actions-menu');
    menu.style.left = `${Math.max(12, Math.min(rect.right - 180, innerWidth - 192))}px`;
    menu.style.top = `${Math.max(12, Math.min(rect.bottom + 5, innerHeight - 114))}px`;
    menu.showPopover(); button.setAttribute('aria-expanded', 'true');
    menu.querySelector('button:not(:disabled)')?.focus();
  }
  function openRenameMeeting() {
    if (!state.manageTarget) return;
    closeMeetingActions();
    $('rename-meeting-title').value = state.manageTarget.title;
    text($('rename-meeting-error'), '');
    $('rename-meeting-dialog').showModal(); $('rename-meeting-title').select();
  }
  async function openDeleteMeeting() {
    const meeting = state.manageTarget;
    if (!meeting) return;
    closeMeetingActions(); state.deletionReady = false;
    $('confirm-delete-meeting').disabled = true;
    text($('delete-meeting-description'), `将永久删除“${meeting.title}”及其全部内容，此操作无法恢复。`);
    text($('delete-meeting-counts'), '正在确认删除范围…'); text($('delete-meeting-error'), '');
    $('delete-meeting-dialog').showModal(); $('cancel-delete-meeting').focus();
    try {
      const impact = await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}/deletion`);
      if (state.manageTarget?.id !== meeting.id || !$('delete-meeting-dialog').open) return;
      text($('delete-meeting-counts'), `当前包含 ${impact.question_count} 个查证问题、${impact.segment_count} 条转写记录、${impact.file_count} 个文件。${impact.audio_busy ? '确认后会先停止本场音频处理，再清除内容。' : '尚未完成的查证和纪要将停止，后续返回结果不再保存。'}`);
      state.deletionReady = true; $('confirm-delete-meeting').disabled = false;
    } catch (error) { text($('delete-meeting-error'), error.message); text($('delete-meeting-counts'), '未能确认删除范围，请取消后重试。'); }
  }
  function forgetMeeting(id) {
    if (state.home) {
      state.home.meetings = state.home.meetings.filter(m => m.id !== id);
      state.home.materials = state.home.materials.filter(d => d.meeting_id !== id);
    }
    state.recentIds = state.recentIds.filter(value => value !== id);
    if (state.meetingId === id) {
      state.request?.controller.abort(); state.request = null; state.selection += 1;
      state.meetingId = ''; state.snapshot = null; state.selectedQuestionId = '';
      state.artifactCache.clear(); state.artifactRequests.clear(); resetSignatures();
      $('meeting-select').value = ''; setRoute('meetings');
    }
    renderMeetingOptions(); renderLibrary();
  }
  async function refreshHome() {
    if (!state.token || state.homeRequest) return;
    const token = state.token;
    const pending = api('/live/v2/home'); state.homeRequest = pending;
    try {
      const home = await pending;
      if (state.token !== token) return;
      state.home = home;
      renderMeetingOptions(); renderCapabilities(); renderLibrary();
    } catch (error) { if (state.token === token) { setSync('列表更新失败，正在重试', true); } }
    finally { if (state.homeRequest === pending) state.homeRequest = null; }
  }
  function setSync(message, error = false) {
    text($('sync-status'), message);
    $('sync-status').dataset.state = error ? 'error' : 'connected';
  }
  async function api(path, options = {}) {
    const {body, ...other} = options;
    const response = await fetch(path, {
      ...other,
      headers: {'Authorization': `Bearer ${state.token}`, ...(body !== undefined ? {'Content-Type': 'application/json'} : {}), ...other.headers},
      ...(body !== undefined ? {body: JSON.stringify(body)} : {}),
      cache: 'no-store',
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const failure = new Error(readable(result.error || result.detail || result.message) || `请求失败（${response.status}）`);
      failure.status = response.status;
      if (response.status === 401) logout(false);
      throw failure;
    }
    return result;
  }
  function resetSignatures() {
    state.detailSignature = '';
    state.transcriptSignature = '';
    state.questionSignature = '';
    state.activitySignature = '';
  }
  function meetingEnded() { return state.snapshot?.meeting?.state !== 'active'; }
  function researchAvailable() { return ['active', 'ended'].includes(state.snapshot?.meeting?.state); }
  function automationHeld(question) {
    const meeting = state.snapshot?.meeting;
    const manual = question.manual_requested ?? (question.origin === 'manual');
    return Boolean(meeting?.research_paused && !manual && (['queued', 'backlog'].includes(question.state) || question.state === 'candidate' && meeting.permissions?.auto_search));
  }
  function selectedQuestion() { return (state.snapshot?.questions || []).find((question) => question.id === state.selectedQuestionId) || null; }
  function questionDocument(question) {
    if (!question) return null;
    const documents = state.snapshot?.documents || [];
    return question.artifact_id
      ? documents.find((document) => document.id === question.artifact_id) || null
      : documents.find((document) => document.question_id === question.id && document.question_version === question.version) || null;
  }
  function artifactID(question) {
    return question?.artifact_id || questionDocument(question)?.id || '';
  }
  function rememberToken(value) {
    try { value ? sessionStorage.setItem('wb_access', value) : sessionStorage.removeItem('wb_access'); } catch { /* Access can be disabled by browser settings. */ }
  }
  function logout(showMessage = true) {
    state.request?.controller.abort();
    state.request = null;
    state.token = '';
    state.home = null;
    state.snapshot = null;
    state.meetingId = '';
    state.transcriptHistory = state.returnNote = null;
    $('transcript-list').replaceChildren();
    state.selectedQuestionId = '';
    state.homeRequest = null;
    state.librarySignature = '';
    state.recentIds = [];
    state.serviceSettings = null; state.settingsRequest = null; state.settingsDirty = false;
    state.clearModelKey = false; state.clearSearchKey = false;
    $('service-model-key').value = ''; $('service-search-key').value = '';
    $('meeting-select').value = '';
    state.selection += 1;
    state.artifactCache.clear();
    state.artifactRequests.clear();
    rememberToken('');
    resetSignatures();
    $('workspace').hidden = true;
    $('login').hidden = false;
    setSync('尚未连接');
    document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
    if (showMessage) notice('已退出工作台');
  }
  async function login(token) {
    state.token = token;
    setSync('正在读取工作区');
    const home = await api('/live/v2/home');
    if (state.token !== token) return;
    state.home = home;
    rememberToken(token);
    $('login').hidden = true;
    $('workspace').hidden = false;
    $('access-code').value = '';
    renderMeetingOptions();
    renderCapabilities();
    setSync('已连接');
    renderSnapshot();
    await readRoute();
    if (nativeAvailable()) nativePost('requestState');
  }
  function renderMeetingOptions() {
    const select = $('meeting-select');
    const options = [new Option('选择会议', '')];
    for (const meeting of state.home?.meetings || []) {
      const suffix = meeting.data_scope === 'synthetic' ? ' · 合成测试' : '';
      options.push(new Option(`${meeting.title || '未命名会议'}${suffix}`, meeting.id));
    }
    select.replaceChildren(...options);
    select.value = state.meetingId;
  }
  function requestLimitsText() {
    const cap = state.home?.capabilities || {};
    if (cap.model_request_limit === null && cap.search_request_limit === null) return '模型与搜索调用不设次数上限。';
    const limit = (name, value) => value === null ? `${name}不限次数` : Number.isFinite(value) ? `${name}累计上限 ${value} 次` : `${name}按本机配置执行`;
    return `${limit('模型', cap.model_request_limit)}，${limit('搜索', cap.search_request_limit)}。`;
  }
  function coordinatorIssue(meeting) {
    if (!meeting) return '';
    if (meeting.coordinator_error && meeting.coordinator_error_message) {
      const details = meeting.coordinator_error_details || {};
      const retry = meeting.coordinator_retry;
      const elapsed = Number.isFinite(details.elapsed_ms) ? `实际耗时 ${(details.elapsed_ms / 1000).toFixed(1)} 秒。` : '';
      const record = Number.isInteger(details.request_id) ? `请求 #${details.request_id}。` : '';
      const recovery = retry?.state === 'scheduled' ? meeting.research_paused ? '自动重试已暂停，恢复自动查证后继续。' : `约 ${Math.max(0, Math.ceil(retry.next_at - Date.now() / 1000))} 秒后自动重试（${retry.attempts}/${retry.max_attempts}）。` : retry?.state === 'running' ? `正在自动重试（${retry.attempts}/${retry.max_attempts}）。` : retry?.state === 'exhausted' ? `已自动重试 ${retry.attempts} 次，仍未恢复，请检查服务后手动重试。` : '';
      return `${meeting.coordinator_error_message} ${elapsed}${record}${recovery} 转写已保存。`;
    }
    const errors = {
      budget_exhausted: '当时配置的调用额度已用尽，问题识别未完成。转写已保存。',
      model_contract_invalid: '模型返回的内容未通过校验，问题识别已停止。转写已保存。',
      request_failed: '模型请求失败，问题识别已停止。请检查模型连接。转写已保存。',
      continuation_no_progress: '模型重复返回相同问题，自动识别已暂停。转写已保存。',
      continuation_limit: '同批发言需要继续分析，自动识别已暂停。转写已保存。',
      context_invalid: '讨论依据已经更正，问题识别已暂停。转写已保存。',
      outcome_unknown: '问题识别未收到完整结果；旧版本未保留具体错误。转写已保存，可单独重试问题识别。'
    };
    return errors[meeting.coordinator_error] || (meeting.coordinator_state === 'outcome_unknown' ? errors.outcome_unknown : meeting.coordinator_state === 'failed' ? '问题识别未完成。请检查服务配置后重试，转写已保存。' : '');
  }
  function renderCapabilities() {
    const cap = state.home?.capabilities || {};
    const meeting = state.snapshot?.meeting;
    const real = meeting?.data_scope === 'real';
    const modelAllowed = meeting?.permissions ? meeting.permissions.model : (cap.model_meeting_ids || []).includes(meeting?.id);
    const searchAllowed = meeting?.permissions ? meeting.permissions.search : (cap.search_meeting_ids || []).includes(meeting?.id);
    const modelState = cap.model_enabled ? real && !modelAllowed ? '本会议待批准' : '已启用' : '未启用';
    const searchState = cap.search_enabled ? real && !searchAllowed ? '本会议待批准' : '已启用' : '未启用';
    const parts = [`模型：${cap.model_configured ? '已配置' : '未配置'} / ${modelState}`, `搜索：${cap.search_configured === undefined ? '配置待确认' : cap.search_configured ? '已配置' : '未配置'} / ${searchState}`];
    text($('capabilities'), parts.join(' · '));
    renderSettings();
  }
  function renderSettings() {
    const cap = state.home?.capabilities || {};
    const native = state.native;
    const desktop = nativeAvailable();
    const modelStates = {unknown:'正在检查中文识别模型…', installed:'中文识别模型已安装 · 音频在本机处理', downloadable:'尚未安装中文识别模型，请先安装。', downloading:'正在安装中文识别模型…', unsupported:'当前系统不支持本机中文识别。'};
    const fileModel = native?.fileModelState || 'unknown';
    text($('settings-audio-state'), desktop ? native?.status || '正在连接本机音频组件…' : '本机收听仅在桌面 App 中可用。浏览器可查看与补充会议。');
    text($('settings-model-state'), desktop ? fileModel === 'downloadable' && native?.modelState === 'installed' ? '系统收听已就绪；文件转写还需安装中文模型。' : modelStates[fileModel] || '模型状态待确认' : '请在桌面 App 中检查与安装。');
    $('check-model').disabled = !desktop || !native?.ready || native?.modelState === 'downloading' || Boolean(native?.meetingId);
    $('install-model').disabled = !desktop || !native?.ready || ![native?.modelState,fileModel].includes('downloadable') || [native?.modelState,fileModel].includes('downloading') || Boolean(native?.meetingId);
    text($('install-model'), native?.modelState === 'installed' && fileModel === 'installed' ? '已安装' : [native?.modelState,fileModel].includes('downloading') ? '安装中…' : '安装模型');
    text($('settings-reasoning-badge'), cap.model_enabled ? '已启用' : cap.model_configured ? '待启用' : '未配置');
    text($('settings-search-badge'), cap.search_enabled ? '已启用' : cap.search_configured ? '待启用' : '未配置');
    text($('settings-authorization'), `新会议默认开启联网查证，可在创建或导入时关闭。自动搜索使用公开检索词。${requestLimitsText()}`);
    renderProviderSettings();
    $('settings-open-folder').disabled = !desktop;
    text($('settings-retention'), '录音、转写、纪要与查证资料长期保存在本机，不会自动到期删除。仅在你确认删除会议时清除该会议资料。');
    $('guide-demo').hidden = state.home?.project?.data_scope !== 'synthetic';
  }

  function providerMessage(id, value, status = '') {
    text($(id), value);
    $(id).dataset.state = status;
  }
  function matchingKeyDestination(kind) {
    const current = state.serviceSettings?.[kind];
    if (!current || current.provider !== $(`service-${kind}-provider`).value) return false;
    try { return new URL(current.base_url).origin === new URL($(`service-${kind}-url`).value).origin; }
    catch { return false; }
  }
  function renderProviderSettings() {
    const settings = state.serviceSettings;
    const owner = state.home?.member?.role === 'owner';
    const busy = state.settingsBusy || Boolean(state.settingsRequest);
    const editable = owner && settings?.editable;
    $('model-service-fields').disabled = !editable || busy;
    $('search-service-fields').disabled = !editable || busy;
    $('archive-settings-fields').disabled = !editable || busy;
    $('save-service-settings').disabled = !editable || busy;
    $('reload-service-settings').disabled = !owner || busy;
    text($('save-service-settings'), state.settingsBusy ? '处理中…' : '保存设置');
    $('model-thinking-row').hidden = $('service-model-provider').value !== 'deepseek';
    let shared = $('service-model-provider').value === 'deepseek' && $('service-search-provider').value === 'deepseek';
    try { shared = shared && new URL($('service-model-url').value).origin === new URL($('service-search-url').value).origin; }
    catch { shared = false; }
    $('search-share-key-row').hidden = !shared;
    if (!shared) $('service-search-shared-key').checked = false;
    $('search-key-field').hidden = shared && $('service-search-shared-key').checked;
    for (const kind of ['model', 'search']) {
      const cleared = kind === 'model' ? state.clearModelKey : state.clearSearchKey;
      const saved = settings?.[kind]?.api_key_configured && matchingKeyDestination(kind);
      text($(`service-${kind}-key-state`), cleared ? '将清除' : saved ? '已保存' : '未配置');
      $(`service-${kind}-key`).placeholder = cleared ? '保存后清除密钥' : saved ? '留空保留已有密钥' : '填写服务商提供的密钥';
      $(`clear-${kind}-key`).disabled = busy || !editable || !saved || cleared;
    }
    if (!owner) providerMessage('service-settings-result', '只有工作区主持人可以修改服务设置。');
    else if (settings && !settings.editable) providerMessage('service-settings-result', '当前运行方式未启用持久化设置，请使用桌面 App 或工作台启动入口。');
  }
  function fillServiceSettings(settings) {
    state.serviceSettings = settings;
    state.settingsDirty = false; state.clearModelKey = false; state.clearSearchKey = false;
    for (const kind of ['model', 'search']) {
      const value = settings[kind];
      $(`service-${kind}-provider`).value = value.provider;
      $(`service-${kind}-url`).value = value.base_url;
      $(`service-${kind}-enabled`).checked = value.enabled;
      $(`service-${kind}-key`).value = '';
    }
    $('service-model-name').value = settings.model.name;
    $('service-model-effort').value = settings.model.reasoning_effort;
    $('service-model-detection').value = settings.model.detection_mode ?? 'fast';
    $('service-model-tokens').value = settings.model.max_tokens;
    $('service-model-timeout').value = settings.model.timeout_seconds ?? 180;
    $('service-model-token-parameter').value = settings.model.token_parameter;
    $('service-model-json').checked = settings.model.json_mode;
    $('service-model-thinking').checked = settings.model.thinking === 'enabled';
    $('service-search-shared-key').checked = settings.search.use_model_key;
    $('keep-original-caf').checked = settings.keep_original_caf === true;
    renderProviderSettings();
  }
  async function loadServiceSettings(force = false) {
    if (state.home?.member?.role !== 'owner' || state.settingsRequest || state.settingsBusy || (!force && state.serviceSettings)) return;
    const token = state.token;
    const request = {};
    state.settingsRequest = request;
    providerMessage('service-settings-result', '正在读取服务设置…');
    renderProviderSettings();
    try {
      const settings = await api('/live/v2/settings');
      if (state.token !== token || state.settingsRequest !== request) return;
      fillServiceSettings(settings);
      providerMessage('service-settings-result', settings.warning || '', settings.warning ? 'error' : '');
      providerMessage('model-test-result', ''); providerMessage('search-test-result', '');
    } catch (error) {
      if (state.token === token) providerMessage('service-settings-result', error.message, 'error');
    } finally {
      if (state.settingsRequest === request) state.settingsRequest = null;
      renderProviderSettings();
    }
  }
  function serviceSettingsBody() {
    return {revision: state.serviceSettings.revision, keep_original_caf: $('keep-original-caf').checked, model: {
      provider: $('service-model-provider').value, base_url: $('service-model-url').value.trim(),
      name: $('service-model-name').value.trim(), enabled: $('service-model-enabled').checked,
      api_key: $('service-model-key').value.trim() || null, clear_api_key: state.clearModelKey,
      json_mode: $('service-model-json').checked, reasoning_effort: $('service-model-effort').value,
      detection_mode: $('service-model-detection').value,
      thinking: $('service-model-thinking').checked ? 'enabled' : 'disabled',
      max_tokens: Number($('service-model-tokens').value), token_parameter: $('service-model-token-parameter').value,
      timeout_seconds: Number($('service-model-timeout').value),
    }, search: {
      provider: $('service-search-provider').value, base_url: $('service-search-url').value.trim(),
      enabled: $('service-search-enabled').checked, api_key: $('service-search-key').value.trim() || null,
      clear_api_key: state.clearSearchKey, use_model_key: $('service-search-shared-key').checked,
    }};
  }
  function editedServiceSettings() {
    state.settingsDirty = true;
    providerMessage('service-settings-result', '有未保存的修改');
    providerMessage('model-test-result', ''); providerMessage('search-test-result', '');
    renderProviderSettings();
  }
  async function testService(kind) {
    if (!state.serviceSettings || state.settingsBusy || !$('service-settings-form').reportValidity()) return;
    const payload = serviceSettingsBody();
    const token = state.token;
    state.settingsBusy = true;
    providerMessage(`${kind}-test-result`, '正在测试…');
    renderProviderSettings();
    try {
      const result = await api(`/live/v2/settings/test/${kind}`, {method: 'POST', body: payload});
      if (state.token !== token) return;
      providerMessage(`${kind}-test-result`, result.message + (result.ok ? ` ${result.elapsed_ms} ms` : ''), result.ok ? 'success' : 'error');
    } catch (error) {
      if (state.token === token) providerMessage(`${kind}-test-result`, error.message, 'error');
    } finally { state.settingsBusy = false; renderProviderSettings(); }
  }
  function renderCapture() {
    const meeting = state.snapshot?.meeting;
    const file = meeting?.input_kind === 'audio_file';
    const synthetic = meeting?.data_scope === 'synthetic' && !file;
    const ended = !meeting || meeting.state !== 'active';
    const desktop = nativeAvailable();
    const native = state.native;
    const bound = native?.meetingId === state.meetingId && Boolean(state.meetingId);
    const moving = bound && ['starting', 'stopping'].includes(native.captureState);
    const busy = bound && Boolean(native.meetingId);
    const listening = bound && native.captureState === 'listening';
    const serverCapture = meeting?.capture || {};
    const inputMode = audioInputs[native?.audioSource] ? native.audioSource : 'system';
    const input = audioInputs[inputMode];
    const inputError = desktop && native?.lastMeetingId === state.meetingId && native?.captureProblem;
    const paused = desktop && native?.lastMeetingId === state.meetingId && native?.captureState === 'stopped';
    const label = synthetic ? '示例体验' : bound ? {starting:'正在连接音频…', listening:`正在收听${input.label}`, stopping:'正在保存最后的转写…', stopped:'正在完成音频收尾…', error:'收听出现异常'}[native.captureState] || native.status : ended ? '收听已结束' : inputError || desktop && native?.captureState === 'error' ? '收听出现异常' : paused ? '收听已暂停' : desktop ? '尚未收听' : captureStates[serverCapture.status] || '尚未收听';
    text($('capture-status'), label);
    if (desktop) $('capture-level').hidden = true;
    $('capture-source-field').hidden = synthetic || file || ended || !desktop;
    $('capture-source').value = inputMode;
    $('meeting-audio-source').value = inputMode;
    $('capture-source').disabled = Boolean(native?.meetingId) || native?.canSelectSource === false;
    $('capture-inputs').hidden = !desktop || synthetic || file || !bound || !['starting', 'listening'].includes(native.captureState);
    for (const source of ['system', 'microphone']) {
      $(`${source}-input`).hidden = !input.sources.includes(source);
      const level = Number(native?.sourceLevels?.[source]) || 0;
      const received = Number(native?.sourceFrames?.[source]) > 0;
      $(`${source}-level`).value = listening ? Math.min(1, Math.max(0, level) * 6) : 0;
      text($(`${source}-input-state`), !received ? '等待输入' : level > 0.002 ? '有声音' : '安静');
    }
    $('open-microphone-settings').hidden = !inputError || !inputError.includes('麦克风权限');
    document.querySelector('.capture-reading').dataset.active = String(listening || (!desktop && ['listening', 'running', 'capturing'].includes(serverCapture.status)));
    $('start-listening').hidden = synthetic || file || ended || busy;
    $('stop-listening').hidden = synthetic || ended || !busy;
    $('start-listening').disabled = !desktop || !native?.ready || state.pendingButtons.has('start-listening');
    $('stop-listening').disabled = moving || !native?.canStop || state.pendingButtons.has('stop-listening');
    text($('start-listening').querySelector('span'), desktop && native?.modelState === 'downloadable' ? '安装中文模型' : '开始收听');
    text($('stop-listening'), file ? '停止导入' : '暂停收听');
    let guidance = input.help + ' 选择后点击“开始收听”。';
    if (synthetic) guidance = '这是一场示例会议，展示转写、查证与资料保存的完整流程。';
    else if (ended) guidance = '本场会议已结束，可继续回看转写与查证资料。';
    else if (!desktop) guidance = '请在桌面 App 中开始收听；此页面可回看资料或手动补充讨论。';
    else if (inputError) guidance = inputError;
    else if (desktop && native?.captureState === 'error') guidance = native.status || '请检查系统录音权限，然后重新开始收听。';
    else if (moving || bound && native.captureState === 'stopped') guidance = native.status || '音频组件正在收尾，请稍候。';
    else if (listening) guidance = input.help;
    else if (native?.modelState === 'downloadable') guidance = '首次使用需要中文识别模型。点击“安装中文模型”前往设置。';
    else if (native?.modelState === 'downloading') guidance = '中文模型正在安装，完成后即可开始收听。';
    else if (native?.modelState === 'unsupported') guidance = '当前系统不支持本机中文识别，可先使用文字补充。';
    else if (serverCapture.status === 'paused' || serverCapture.status === 'stopped') guidance = '收听已暂停，可切换音源后继续。';
    if (desktop && bound && !moving && listening) guidance += ' 切换音源请先暂停。';
    text($('capture-guidance'), guidance);
    text($('transcript-live-label'), synthetic ? '示例讨论' : listening ? '实时更新中' : ended ? '会议记录' : '等待收听');
    $('import-progress').hidden = !file;
    if (file) {
      const job = meeting.audio_import;
      const labels = {processing:'正在转写', completed:'转写完成', cancelled:'导入已停止', failed:'导入失败', interrupted:'导入已中断'};
      text($('capture-status'), bound && native.captureState === 'stopping' ? '正在保存转写…' : job ? labels[job.state] || job.state : bound ? '正在准备导入' : '等待导入音频');
      text($('capture-guidance'), job?.error || (ended ? '本场会议已结束，录音与文档可在“会议文件”查看。恢复自动查证可继续分析剩余转写和未完成问题。' : job?.state === 'completed' ? '录音已转写；问题分析与查证可能尚未完成。结束会议会暂停自动任务，会后可恢复。' : job?.state === 'processing' || bound ? '直接读取文件，在本机识别。无需播放，不会收听电脑上的其他声音。' : '选择音频开始转写；停止后会保留已识别的内容。'));
      if (job?.transcript_state === 'save_failed') text($('capture-guidance'), '转写内容已保留，但自动保存文件失败。可点击“导出完整转写”另存，或检查会议目录的写入权限。');
      text($('import-file-name'), job?.file_name || '尚未绑定音频');
      text($('import-progress-label'), job ? `${labels[job.state] || job.state} · ${audioTime(job.processed_ms)} / ${audioTime(job.duration_ms)}` : bound ? native.status : native?.captureState === 'error' ? native.status : '选择音频后开始');
      $('import-progress-bar').value = job?.duration_ms ? Math.min(100, job.processed_ms / job.duration_ms * 100) : 0;
      $('restart-import').hidden = ended || job?.state === 'processing' || Boolean(bound);
      text($('restart-import'), job ? '重新导入为新会议' : '重新选择音频');
      $('download-transcript').disabled = !(state.snapshot?.segments || []).some(s => s.type === 'transcript.final') || state.pendingButtons.has('download-transcript');
      text($('transcript-live-label'), state.snapshot?.segment_count > 120 ? '显示最近 120 段 · 完整内容可导出' : job?.state === 'processing' ? '转写逐段更新' : '完整内容可导出');
      $('capture-level').hidden = true;
      document.querySelector('.capture-reading').dataset.active = String(job?.state === 'processing');
    }
    const cap = state.home?.capabilities || {};
    const granted = meeting && (meeting.permissions ? meeting.permissions.model && meeting.permissions.search : (cap.model_meeting_ids || []).includes(meeting.id) && (cap.search_meeting_ids || []).includes(meeting.id));
    const available = cap.model_enabled && cap.search_enabled && granted;
    $('research-warning').hidden = !meeting || synthetic || available;
    $('authorize-research').hidden = !researchAvailable() || !cap.model_enabled || state.home?.member?.role !== 'owner';
    text($('research-warning-text'), cap.model_enabled && cap.search_enabled ? '本场会议尚未授权联网查证。转写可在本机继续。' : '联网查证尚未启用。可先在本机转写，前往设置连接模型与搜索。');
    const issue = coordinatorIssue(meeting);
    const researchState = meeting?.coordinator_state;
    const postMeetingWork = state.snapshot?.automation_progress?.pending || researchState === 'running' || state.snapshot?.questions?.some(q => activeStates.has(q.state));
    text($('questions-guidance'), ended ? !researchAvailable() ? '会议内容已清理，无法继续查证。' : issue ? '会后问题识别受阻，原因见上方提示。已有问题仍可单独查证。' : meeting?.research_paused ? '会议已结束，自动任务已暂停。恢复后会从剩余转写继续识别问题；已有问题也可单独重试。' : postMeetingWork ? '正在处理会后任务：继续分析剩余转写，并查证未完成的问题。' : '本场转写已分析完毕。可查看已有资料，或打开具体问题重试。' : meeting?.research_paused ? '自动查证已暂停。打开等待中的问题可立即查证，也可以手动添加或重试。' : ['scheduled', 'running'].includes(meeting?.coordinator_retry?.state) ? '正在恢复问题识别，将从未处理的发言继续。已有问题可照常查证。' : issue ? '自动问题识别受阻，原因见上方提示。已有问题仍可手动查证。' : synthetic ? '打开问题查看参考回答、来源与 Markdown。示例内容仅用于体验。' : !available ? '联网查证尚未就绪。你可以先添加问题，配置服务后再查证。' : researchState === 'running' ? '正在分析讨论，识别待查证的问题…' : researchState === 'pending' ? '等待分析新发言，识别后会显示在这里。' : '“调研一下”“找些来源”等明确请求会自动识别，也可以手动添加。');
    $('open-meeting-folder').disabled = !desktop || !meeting?.directory;
    renderRecent();
  }
  async function selectMeeting(id, questionId = '', tab = 'answer', updateHistory = true) {
    if (state.native?.meetingId && state.native.meetingId !== id) {
      state.switchTarget = {id, questionId, tab};
      text($('switch-error'), '');
      $('switch-dialog').showModal();
      return false;
    }
    if (id === state.meetingId && state.snapshot) {
      if (questionId) { state.selectedQuestionId = questionId; state.detailTab = tab; state.detailSignature = ''; renderDetail(); }
      setMeetingTab(questionId ? 'results' : 'transcript', false);
      setRoute('meeting', updateHistory);
      if (questionId) revealAnswer();
      return true;
    }
    state.request?.controller.abort();
    for (const id of ['meeting-info-dialog', 'activity-dialog']) { if ($(id).open) $(id).close(); }
    state.request = null;
    state.meetingId = id;
    state.selection += 1;
    state.queueFilter = 'all';
    $('follow-transcript').checked = true;
    $('work-grid').dataset.leftPane = 'transcript';
    state.transcriptHistory = state.returnNote = null;
    state.snapshot = null;
    state.selectedQuestionId = questionId;
    state.detailTab = tab;
    $('meeting-file-preview').hidden = true;
    text($('archive-error'), '');
    $('meeting-select').value = id;
    $('transcript-input').value = '';
    resetSignatures();
    renderSnapshot();
    setMeetingTab(questionId ? 'results' : 'transcript', false);
    setRoute('meeting', updateHistory);
    state.recentIds = [id, ...state.recentIds.filter(item => item !== id)].slice(0, 4);
    if (id) await refresh();
    if (questionId) revealAnswer();
    if (nativeAvailable()) nativePost('requestState');
    return true;
  }
  async function refresh() {
    if (!state.token || !state.meetingId || state.request) return;
    const request = {meetingId: state.meetingId, selection: state.selection, controller: new AbortController()};
    state.request = request;
    try {
      const snapshot = await api(`/live/v2/meetings/${encodeURIComponent(request.meetingId)}/snapshot`, {signal: request.controller.signal});
      if (state.request !== request || request.selection !== state.selection || request.meetingId !== state.meetingId) return;
      state.snapshot = snapshot;
      state.snapshotReceivedAt = Date.now();
      state.serverOffset = typeof snapshot.server_time === 'number' ? Date.now() - snapshot.server_time * 1000 : 0;
      if (snapshot.capabilities) {
        state.home.capabilities = snapshot.capabilities;
        renderCapabilities();
      }
      renderSnapshot();
      setSync('已连接');
      text($('last-sync'), `同步于 ${time(Date.now())}`);
    } catch (error) {
      if (error.name !== 'AbortError' && request.selection === state.selection && state.token) {
        setSync('同步中断，正在重试', true);
        text($('last-sync'), `未能更新：${error.message}`);
      }
    } finally { if (state.request === request) state.request = null; }
  }
  function renderSnapshot() {
    const snapshot = state.snapshot;
    const meeting = snapshot?.meeting;
    text($('session-title'), meeting?.title || '会议工作台');
    const selected = Boolean(state.meetingId);
    const live = Boolean(meeting) && !meetingEnded();
    const research = researchAvailable();
    const afterMeeting = meeting?.state === 'ended';
    text($('meeting-state'), meeting ? meetingStates[meeting.state] || meeting.state || '状态未知' : selected ? '正在读取会议' : '尚未选择');
    if (meeting?.state === 'active' && meeting.research_paused) text($('meeting-state'), '会议进行中');
    $('meeting-state').dataset.state = meeting?.state || 'paused';
    const capture = meeting?.capture || {};
    const level = typeof capture.level === 'number' && Number.isFinite(capture.level) ? Math.min(1, Math.max(0, capture.level)) : null;
    $('capture-level').hidden = level === null;
    $('capture-level').value = level || 0;
    $('capture-level').setAttribute('aria-valuetext', level === null ? '未提供电平' : `实际电平 ${Math.round(level * 100)}%`);
    text($('capture-detail'), [capture.updated_at ? `更新于 ${time(capture.updated_at)}` : '', capture.code || ''].filter(Boolean).join(' · '));
    const segments = snapshot?.segments || [];
    text($('transcript-count'), snapshot?.segment_count ?? segments.filter(item => !retracted(item)).length);
    text($('meeting-date'), meeting ? `创建于 ${calendarDate(meeting.created_at)}` : '');
    if (state.route === 'meeting') text($('breadcrumb'), meeting?.title || '正在读取会议');
    text($('directory'), snapshot?.output_directory || (selected ? '正在读取目录' : '选择会议后显示'));
    $('directory').title = snapshot?.output_directory || '';
    const synthetic = meeting?.data_scope === 'synthetic' && meeting?.input_kind !== 'audio_file';
    $('scope-notice').hidden = !synthetic;
    text($('scope-notice'), synthetic ? '示例会议' : '');
    $('demo-controls').hidden = !synthetic || Boolean(segments.length || snapshot?.questions?.length);
    $('run-demo').disabled = !live || Boolean(segments.length || snapshot?.questions?.length) || state.pendingButtons.has('run-demo');
    for (const id of ['transcript-input', 'transcript-submit', 'compose-question', 'new-question', 'end-meeting']) $(id).disabled = !live || state.pendingButtons.has(id);
    $('research-control').disabled = !research || state.pendingButtons.has('research-control');
    if ((!research || !live && !state.dialogQuestion) && $('question-dialog').open) $('question-dialog').close();
    if (!research && $('search-query-dialog').open) $('search-query-dialog').close();
    text($('research-control-label'), meeting?.research_paused ? '恢复自动查证' : '暂停自动查证');
    $('research-control-icon').setAttribute('href', meeting?.research_paused ? '#i-play' : '#i-pause');
    const paused = Boolean(meeting?.research_paused);
    const questions = snapshot?.questions || [];
    const unfinished = questions.filter(q => activeStates.has(q.state)).length;
    const progress = snapshot?.automation_progress;
    const pendingSegments = Math.max(0, progress?.pending || 0);
    const issue = coordinatorIssue(meeting);
    const recovering = ['scheduled', 'running'].includes(meeting?.coordinator_retry?.state);
    const postMeetingStatus = issue && !recovering ? '会后问题识别待重试' : pendingSegments || meeting?.coordinator_state === 'running' ? paused ? '会后问题识别已暂停' : recovering ? '正在恢复会后问题识别' : meeting?.coordinator_state === 'running' ? '会后问题识别进行中' : '会后问题识别等待执行' : questions.some(q => q.state === 'running') ? '会后查证进行中' : unfinished ? paused ? '未完成查证已暂停' : '会后查证等待执行' : questions.some(q => ['failed', 'outcome_unknown'].includes(q.state)) ? '查证问题可重试' : questions.length ? '查证已完成' : '暂无查证问题';
    text($('automation-status'), !meeting ? '正在读取查证状态' : afterMeeting ? postMeetingStatus : !live ? meetingStates[meeting.state] || '会议不可用' : paused ? '自动查证已暂停' : recovering ? '正在恢复问题识别' : meeting.coordinator_error || ['failed', 'outcome_unknown'].includes(meeting.coordinator_state) ? '问题识别待重试' : '自动查证运行中');
    $('automation-status').dataset.state = !research ? 'ended' : paused ? 'paused' : issue && !recovering ? 'failed' : 'active';
    $('automation-hint').hidden = !research || !paused && !(afterMeeting && pendingSegments);
    text($('automation-hint'), afterMeeting ? `${pendingSegments ? `还有 ${pendingSegments} 段转写待分析。` : ''}${paused ? '恢复自动查证后继续识别剩余转写和未完成问题；也可单独重试已有问题。' : issue && !recovering ? '问题识别暂未完成，请查看错误提示并重试；已完成资料会保留。' : '将从剩余转写继续分析，已完成的资料会保留。'}` : '已开始的任务会完成；仍可手动添加、立即查证或重试。');
    text($('meeting-info-guidance'), afterMeeting ? '会议已停止收听并整理文件。恢复自动查证后，从未分析的转写继续识别问题，并处理未完成查证；已完成资料会保留。' : '暂停只影响自动任务。已开始的查证会继续，手动添加和重试仍可用；恢复后从未处理的转写继续。');
    text($('automation-progress-label'), '问题识别进度');
    text($('automation-progress'), progress ? `已分析 ${progress.processed} 段 · 待分析 ${progress.pending} 段${afterMeeting ? ` · ${unfinished} 个问题尚待完成` : progress.held_questions ? ` · ${progress.held_questions} 个问题等待恢复` : ''}` : '正在读取');
    for (const id of ['show-meeting-info', 'show-activity']) $(id).disabled = !meeting;
    $('coordinator-warning').hidden = !research || !issue;
    text($('coordinator-warning-text'), issue ? `${issue}${paused && !recovering ? ' 恢复自动查证后，可单独重试问题识别。' : ''}` : '');
    $('retry-coordinator').hidden = !research;
    text($('retry-coordinator'), recovering ? '立即重试' : '重试问题识别');
    $('retry-coordinator').disabled = !research || paused || meeting?.coordinator_state === 'running' || state.pendingButtons.has('retry-coordinator');
    renderCapabilities();
    renderCapture();
    renderTranscripts(segments);
    renderQuestions(snapshot?.questions || []);
    renderDetail();
    renderActivity(snapshot?.activity || []);
    renderMeetingFiles();
  }
  let filePreviewRequest = 0;
  async function previewMeetingFile(mid, kind, scroll = false) {
    const n = ++filePreviewRequest;
    const current = () => n === filePreviewRequest && state.meetingId === mid && state.route === 'meeting' && ['files','summary'].includes($('work-grid').dataset.leftPane);
    text($('archive-error'), '');
    try {
      const value = await api(`/live/v2/meetings/${encodeURIComponent(mid)}/files/${kind}`);
      if (!current()) return;
      text($('meeting-file-title'), kind === 'summary' ? '会议纪要' : '全量转录');
      renderMeetingMarkdown(value.content); $('meeting-file-preview').hidden = false;
      if (scroll) $('meeting-file-preview').scrollIntoView({block:'nearest'});
    } catch (error) { if (current()) text($('archive-error'), error.message); }
  }
  function renderMeetingFiles() {
    const meeting = state.snapshot?.meeting;
    if (!meeting) return;
    const list = $('meeting-files-list');
    const signature = JSON.stringify([meeting.id, meeting.state, meeting.recordings, meeting.base_files, meeting.summary]);
    if (list.dataset.signature === signature) return;
    list.dataset.signature = signature;
    const ended = meeting.state === 'ended';
    const files = meeting.base_files || {};
    const recordings = meeting.recordings || [];
    const saved = recordings.filter(r => ['saved', 'partial'].includes(r.state));
    const recording = recordings.some(r => ['recording', 'copying'].includes(r.state));
    const missing = recordings.some(r => ['missing', 'failed', 'partial', 'copy_failed'].includes(r.state));
    const archiving = recordings.some(r => ['pending', 'converting', 'cleanup'].includes(r.archive_state));
    const archiveFailed = recordings.some(r => ['failed', 'cleanup_failed'].includes(r.archive_state));
    const summaryRunning = ['pending', 'running'].includes(meeting.summary?.state);
    const complete = saved.length > 0 && recordings.every(r => r.state === 'saved' && (!r.archive_state || r.archive_state === 'saved')) && files.transcript?.state === 'saved' && files.summary?.state === 'saved' && meeting.summary?.state === 'completed';
    text($('archive-status'), ended ? complete ? '已归档' : missing ? '录音不完整' : archiveFailed ? '录音归档失败' : !saved.length ? '录音未保存' : archiving || summaryRunning ? '正在归档' : '资料已保存 · 请核对下方状态' : '会后自动归档');
    text($('archive-guidance'), ended ? !saved.length ? '本场没有录音文件；此前未录下的音频无法从文字恢复。已保存文件可在下方查看。' : '录音和已保存的文档位于本场会议文件夹，状态见下方。' : '收听时同步保存原始音频；结束会议后自动保存完整转录并整理纪要。');
    const names = {pending:'等待生成 AI 总结', running:'正在整理 AI 总结', completed:'AI 总结已保存，请核对', failed:'AI 总结失败 · 已保留本机摘录', outcome_unknown:'AI 请求被中断 · 可重试', permission_revoked:'本场模型授权已关闭 · 已停止总结并保留本机摘录；重新授权后可重试', local_only:'本机摘录已保存 · 未启用 AI 总结'};
    if (!ended && meeting.permissions?.model) names.local_only = '本机摘录已保存 · 结束后整理纪要';
    const row = (title, status) => {
      const node = element('div', 'meeting-file-row');
      const copy = element('div'); copy.append(element('h3', '', title), element('p', '', status));
      const actions = element('div', 'meeting-file-actions'); node.append(copy, actions); return {node, copy, actions};
    };
    const audio = row('01  会议录音', recording ? '正在保存到本机' : saved.length ? `${saved.length} 份录音${saved.some(r => r.state === 'partial') ? ' · 部分录音被中断' : ' · 已保存'}` : ended ? '本场没有已保存的录音' : '开始收听或导入音频后保存');
    if (recordings.length) {
      const clips = element('ul', 'meeting-recordings');
      recordings.forEach((r, index) => {
        const item = element('li'); const format = (r.path || '').split('.').pop().toUpperCase();
        item.dataset.recordingState = r.state;
        const label = `${({system:'电脑声音',microphone:'麦克风',file:'导入原音频'})[r.source] || '录音'}${recordings.length > 1 ? ` ${index + 1}` : ''}`;
        if (!['saved', 'partial'].includes(r.state)) {
          const reason = r.state === 'missing' ? r.frames_received === 0 ? '未收到音频数据，未生成录音文件' : '未生成录音文件或文件已缺失' : ({recording:'正在录音', copying:'正在保存导入音频', failed:'录音保存失败', copy_failed:'导入音频保存失败'})[r.state] || '录音尚未保存';
          item.append(element('strong', '', label), element('span', '', ` · ${reason}`));
          if (r.error) item.append(element('span', '', ` · ${readable(r.error)}`));
          clips.append(item); return;
        }
        const button = element('button', '', `${label} · ${format} · 打开播放`);
        button.disabled = !nativeAvailable(); button.addEventListener('click', () => nativePost('openMeetingAudio', meeting.id, r.id));
        item.append(button);
        if (r.state === 'partial') item.append(element('span', '', ' · 录音中断，已保留可用部分'));
        if (r.archive_error) item.append(element('span', '', `${r.archive_error} · ${r.archive_state === 'cleanup_failed' ? 'M4A 已保存，原件清理未完成' : '保留 CAF'}`));
        else if (ended && r.state === 'saved' && ['pending','converting','cleanup'].includes(r.archive_state)) item.append(element('span', '', ' 正在归档 M4A'));
        else if (r.raw_path && r.keep_original_caf) item.append(element('span', '', ' 原始 CAF 已保留'));
        clips.append(item);
      });
      audio.copy.append(clips);
      if (ended && saved.some(r => ['failed','cleanup_failed'].includes(r.archive_state)) && state.home?.member?.role === 'owner') {
        const retry = element('button', '', '重试录音归档');
        retry.addEventListener('click', async () => {
          retry.disabled = true;
          try { await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}/files/retry`, {method:'POST',body:{}}); await refreshNow(); }
          catch (error) { text($('archive-error'), error.message); retry.disabled = false; }
        });
        audio.actions.append(retry);
      }
    }
    const transcript = row('02  全量转录 · Markdown', files.transcript?.state === 'saved' ? '完整保存，包含全部最终转写片段' : files.transcript?.state === 'save_failed' ? '保存失败 · 可先导出转写，检查目录写入权限' : ended ? '旧版会议尚未归档，可导出转写' : '结束会议后自动保存');
    const exportTranscript = element('button', '', '导出转录');
    exportTranscript.addEventListener('click', async () => {
      try { const value = await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}/transcript`); downloadMarkdown(value.content, value.filename); }
      catch (error) { text($('archive-error'), error.message); }
    });
    transcript.actions.append(exportTranscript);
    const summary = row('03  会议纪要', files.summary?.state === 'save_failed' ? '纪要文件保存失败 · 请检查目录写入权限' : names[meeting.summary?.state] || (ended ? '旧版会议尚未生成纪要' : '结束会议后整理讨论要点、明确决定与后续行动'));
    summary.node.dataset.fileKind = 'summary';
    for (const [kind, target] of [['transcript', transcript], ['summary', summary]]) {
      if (!files[kind]) continue;
      const preview = element('button', '', '查看' + (kind === 'summary' ? '纪要' : '转录'));
      preview.addEventListener('click', () => previewMeetingFile(meeting.id, kind, true));
      target.actions.prepend(preview);
      if (kind === 'summary') {
        const exportSummary = element('button', '', '导出纪要');
        exportSummary.addEventListener('click', async () => {
          try { const value = await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}/files/summary`); downloadMarkdown(value.content, value.filename); }
          catch (error) { text($('archive-error'), error.message); }
        });
        target.actions.append(exportSummary);
      }
      if (ended && files[kind]?.state === 'save_failed') {
        const save = element('button', '', '重新保存');
        save.addEventListener('click', async () => {
          save.disabled = true;
          try { await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}/files/retry`, {method:'POST',body:{}}); await refreshNow(); }
          catch (error) { text($('archive-error'), error.message); save.disabled = false; }
        });
        target.actions.append(save);
      }
    }
    if (ended && meeting.permissions?.model && state.home?.capabilities?.model_enabled && !['pending','running'].includes(meeting.summary?.state)) {
      const retry = element('button', '', meeting.summary?.state === 'completed' ? '重新整理' : '生成 AI 总结');
      retry.addEventListener('click', async () => {
        retry.disabled = true; text($('archive-error'), '');
        try { await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}/summary`, {method:'POST', body:{}}); await refreshNow(); }
        catch (error) { text($('archive-error'), error.message); retry.disabled = false; }
      });
      summary.actions.append(retry);
    }
    list.replaceChildren(audio.node, transcript.node, summary.node);
  }
  function renderMeetingMarkdown(content) {
    const fragment = document.createDocumentFragment();
    for (const line of content.split('\n')) {
      if (!line.trim()) continue;
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) fragment.append(element(`h${Math.min(heading[1].length + 1, 4)}`, '', heading[2]));
      else if (/^\[查看全量转录\]\(/.test(line)) continue;
      else fragment.append(element(line.startsWith('> ') ? 'blockquote' : 'p', '', line.replace(/^>\s*/, '').replace(/^-\s*/, '• ')));
    }
    $('meeting-file-content').replaceChildren(fragment);
  }
  function transcriptPage() { return state.transcriptHistory?.page ?? state.snapshot?.transcript_page; }
  function visibleTranscripts(latest) {
    const history = state.transcriptHistory;
    if (!history) return latest;
    const {revision_epoch: epoch, changes = []} = state.snapshot.transcript_page;
    if (history.epoch !== epoch) {
      const missed = changes.filter(change => change.epoch > history.epoch);
      if (missed.length !== epoch - history.epoch) { state.transcriptHistory = null; return latest; }
      for (const change of missed) {
        if (history.rows.get(change.segment_id)?.revision < change.revision) history.rows.delete(change.segment_id);
      }
      history.epoch = epoch;
    }
    for (const segment of latest) {
      const old = history.rows.get(segment.segment_id);
      if (!old || segment.revision >= old.revision) history.rows.set(segment.segment_id, segment);
    }
    return [...history.rows.values()].sort((a, b) => a.event_seq - b.event_seq);
  }
  async function earlierTranscripts(focusPage = true) {
    const page = transcriptPage();
    if (!page?.has_more || !Number.isSafeInteger(page.next_before) || state.transcriptHistory?.loading) return;
    const mid = state.meetingId;
    const history = state.transcriptHistory ||= {epoch: page.revision_epoch, rows: new Map(), page, loading: false};
    visibleTranscripts(state.snapshot.segments);
    history.loading = true;
    $('older-transcript').disabled = true;
    try {
      const result = await api(`/live/v2/meetings/${encodeURIComponent(mid)}/segments?before=${page.next_before}`);
      if (history !== state.transcriptHistory || mid !== state.meetingId) return;
      if (history.epoch !== result.revision_epoch) { await refreshNow(); return; }
      for (const s of result.segments) if (!history.rows.has(s.segment_id) || s.revision > history.rows.get(s.segment_id).revision) history.rows.set(s.segment_id, s);
      history.page = {has_more: result.has_more, next_before: result.next_before, revision_epoch: result.revision_epoch};
      history.loading = false;
      renderTranscripts(state.snapshot.segments, true);
      // Start reading the newly requested page at its first utterance.
      const first = [...$('transcript-list').children].find(row => row.dataset.segmentId === result.segments[0]?.segment_id);
      if (focusPage && first && state.route === 'meeting' && transcriptVisible()) {
        first.tabIndex = -1; first.focus({preventScroll:true}); first.scrollIntoView({block:'start'});
      }
    } catch (err) { if (history === state.transcriptHistory) notice(err.message, true); }
    finally { if (history === state.transcriptHistory) { history.loading = false; $('older-transcript').disabled = false; } }
  }
  function transcriptNote(q) {
    const terminal = ['completed', 'insufficient'].includes(q.state) && !q.basis_stale;
    const result = terminal ? q.result : null;
    const progress = q.progress?.version === q.version ? q.progress : null;
    let label = automationHeld(q) ? '等待恢复' : researchLabel(q);
    let excerpt = '';
    if (result) {
      label = result.original_evidence?.label || evidenceStates[result.evidence_status] || label;
      excerpt = result.brief?.short_answer || (result.answer || '').split(/\n/).find(Boolean) || '';
    } else if (['failed', 'outcome_unknown'].includes(q.state)) {
      excerpt = q.error || q.error_details?.message || q.error_details?.label || '没有记录到更具体的失败原因。';
    } else if (q.state === 'running') {
      excerpt = q.stage === 'searching' && progress?.query ? `检索词：${progress.query}` : label;
    } else { excerpt = q.stage && !Object.hasOwn(stages, q.stage) ? q.stage : label; }
    return {id:q.id, version:q.version, title:q.title, label, excerpt, state:q.state, original:result?.original_evidence || null};
  }
  function renderOriginalEvidence(container, evidence, compact = false) {
    const signature = JSON.stringify(evidence || null);
    if (signature === container.dataset.signature) return;
    container.dataset.signature = signature;
    container.replaceChildren();
    container.hidden = !evidence;
    if (!evidence) return;
    container.dataset.originalState = evidence.state;
    container.append(element(compact ? 'strong' : 'h3', 'original-label', evidence.label));
    if (evidence.quote) container.append(element('blockquote', 'original-quote', evidence.quote));
    if (evidence.url) container.append(sourceLink(evidence, [evidence.publisher, evidence.title, evidence.locator].filter(Boolean).join(' · ') || '打开来源'));
    if (!compact || !evidence.quote) container.append(element('p', 'original-message', evidence.message));
  }
  function updateNote(node, note) {
    node.dataset.state = note.state;
    text(node.querySelector('.note-title'), note.title);
    text(node.querySelector('.note-status'), note.label);
    text(node.querySelector('.note-answer'), note.excerpt);
    node.querySelector('.note-answer').hidden = !note.excerpt;
    const evidence = node.querySelector('.note-proof');
    renderOriginalEvidence(evidence, note.original, true);
  }
  function makeNote(note) {
    const node = element('details', 'transcript-note'); node.dataset.noteId = note.id;
    const summary = element('summary');
    summary.append(icon('question'), element('span', 'visually-hidden', '查证便签：'), element('span','note-title'), element('span','note-status'), icon('chevron','note-chevron'));
    const body = element('div','note-body');
    const button = element('button','text-button note-full','查看完整查证'); button.type = 'button'; button.append(icon('arrow'));
    body.append(element('p','note-answer'), element('div','note-proof'), button); node.append(summary, body);
    return node;
  }
  function renderTranscripts(latest, paging = false) {
    const segments = visibleTranscripts(latest).filter(segment => !retracted(segment) && segment.text?.trim());
    const page = transcriptPage();
    $('transcript-paging').hidden = !page?.has_more && !state.transcriptHistory;
    $('older-transcript').hidden = !page?.has_more;
    $('older-transcript').disabled = Boolean(state.transcriptHistory?.loading);
    $('latest-transcript').hidden = !state.transcriptHistory;
    const segmentMap = new Map(segments.map(s => [s.segment_id, s]));
    const notes = new Map();
    for (const q of state.snapshot?.questions || []) {
      if (q.basis_stale || q.state === 'superseded') continue;
      // A single stable anchor is used even if multiple utterances supplied context.
      // Never substitute a nearby visible utterance when the anchor is offscreen.
      const ref = q.anchor_ref || q.source_refs?.[0];
      const s = ref && segmentMap.get(ref.segment_id);
      if (s?.type !== 'transcript.final' || s.revision !== ref.revision) continue;
      if (!notes.has(s.segment_id)) notes.set(s.segment_id, []);
      notes.get(s.segment_id).push(transcriptNote(q));
    }
    const signature = JSON.stringify([segments, [...notes], state.meetingId, state.snapshot?.meeting?.data_scope, state.native?.captureState, state.native?.audioSource, state.snapshot?.meeting?.audio_import?.state]);
    if (signature === state.transcriptSignature) return;
    state.transcriptSignature = signature;
    const list = $('transcript-list');
    const viewport = transcriptViewport();
    const atBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 48;
    const active = document.activeElement;
    const reading = !$('follow-transcript').checked || list.contains(active) || Boolean(list.querySelector('.transcript-note[open]')) || Boolean(state.transcriptHistory);
    const showing = transcriptVisible();
    const visibleTop = Math.max(viewport.getBoundingClientRect().top, document.querySelector('.meeting-toolbar').getBoundingClientRect().bottom);
    const focusedInView = list.contains(active) && active !== list && active.getBoundingClientRect().bottom > visibleTop;
    const anchor = showing && !paging && (!atBottom || reading) && (focusedInView ? active : [...list.children].find(row => row.getBoundingClientRect().bottom > visibleTop));
    const anchorY = anchor && anchor.getBoundingClientRect().top;
    if (!segments.length) {
      const file = state.snapshot?.meeting?.input_kind === 'audio_file';
      const importing = state.snapshot?.meeting?.audio_import?.state === 'processing' || state.native?.meetingId === state.meetingId;
      const listeningHelp = state.native?.audioSource === 'microphone' ? '请对着麦克风讲话，转写会逐段出现在这里。' : state.native?.audioSource === 'both' ? '会议播放和你的麦克风发言，会分别标注音源并显示在这里。' : '电脑上有会议声音时，转写会出现在这里。';
      empty(list, file ? importing ? '正在识别音频，转写会逐段出现在这里。' : '暂未识别到语音。可以重新导入，或手动补充讨论。' : state.snapshot?.meeting?.data_scope === 'synthetic' ? '点击上方“播放示例流程”，查看一段讨论如何变成资料。' : state.native?.captureState === 'listening' ? listeningHelp : '开始收听后，这里会逐段显示讨论。也可以在下方手动补充一段原话。');
      return;
    }
    list.querySelector(':scope > .empty-state')?.remove();
    const rows = new Map([...list.children].map(node => [node.dataset.segmentId, node]));
    // Remove rolled-out rows first: inserting/moving retained nodes detaches their focused children.
    for (const [id, node] of rows) if (!segmentMap.has(id)) node.remove();
    let rowIndex = 0;
    for (const segment of segments) {
      const id = segment.segment_id;
      const item = rows.get(id) || element('article','transcript-item');
      if (!rows.has(id)) { item.dataset.segmentId = id; item.append(element('div','transcript-meta'), element('p','transcript-text'), element('div','transcript-notes')); }
      if (list.children[rowIndex++] !== item) list.insertBefore(item, list.children[rowIndex - 1] || null);
      const source = segmentSource(segment);
      const metaKey = JSON.stringify([segmentTime(segment), segment.audio_start_ms, segment.type, source]);
      if (item.dataset.metaKey !== metaKey) {
        item.dataset.metaKey = metaKey;
        const meta = item.firstElementChild;
        const stamp = element('time','',segment.source === 'file' ? audioTime(segment.audio_start_ms) : time(segmentTime(segment)));
        const occurred = date(segmentTime(segment)); if (occurred) stamp.dateTime = occurred.toISOString();
        const identity = element('span','source-identity');
        const glyph = {'电脑声音':'sys','麦克风':'mic','音频文件':'audiofile','文字补充':'manual'}[source] || 'wave';
        identity.append(icon(glyph), document.createTextNode(source));
        meta.replaceChildren(identity, stamp);
        if (segment.type === 'transcript.interim') meta.append(element('span','revision','临时识别'));
      }
      text(item.children[1], segment.text);
      const group = item.lastElementChild, desired = notes.get(id) || [];
      const existing = new Map([...group.children].map(node => [node.dataset.noteId, node]));
      for (const [key, node] of existing) if (!desired.some(n => n.id === key)) node.remove();
      desired.forEach((n,i) => {
        const node = existing.get(n.id) || makeNote(n);
        if (group.children[i] !== node) group.insertBefore(node, group.children[i] || null);
        const key = JSON.stringify(n);
        if (key !== node.dataset.signature) { updateNote(node,n); node.dataset.signature = key; }
      });
    }
    highlightQuestionNote();
    if (showing && !paging) {
      if (anchor?.isConnected) viewport.scrollTop += anchor.getBoundingClientRect().top - anchorY;
      else if (atBottom && !reading) viewport.scrollTop = viewport.scrollHeight;
    }
  }
  function queueGroup(question) {
    if (automationHeld(question) || ['needs_review','paused','failed','backlog','outcome_unknown'].includes(question.state) || question.state === 'candidate' && !state.snapshot?.meeting?.permissions?.auto_search) return 'action';
    if (['queued','running','waiting_dependency','candidate'].includes(question.state)) return 'working';
    if (['completed','insufficient','superseded','cancelled'].includes(question.state)) return 'done';
    return 'action';
  }
  function stateGlyph(q) {
    if (automationHeld(q)) return 'hold';
    if (q.basis_stale) return 'unknown';
    if (['completed','insufficient'].includes(q.state)) return ({supported:'check', mixed:'mixed', insufficient:'missing', model_explanation:'unknown'})[q.result?.evidence_status] || (q.state === 'insufficient' ? 'missing' : 'none');
    return ({failed:'failed',outcome_unknown:'unknown',running:'running',candidate:'candidate',paused:'hold',queued:'wait',waiting_dependency:'wait',needs_review:'unknown'})[q.state] || 'none';
  }
  function renderQuestions(questions) {
    if (state.snapshot && !questions.some((question) => question.id === state.selectedQuestionId)) state.selectedQuestionId = questions[0]?.id || '';
    text($('question-count'), questions.length);
    const automatic = Boolean(state.snapshot?.meeting?.permissions?.auto_search);
    const signature = JSON.stringify(questions.map((question) => [question.id, question.title, question.state, question.stage, question.progress?.model?.phase, question.error, question.error_details, question.priority, question.version, question.created_at, question.result?.evidence_status, question.basis_stale, automationHeld(question)])) + state.selectedQuestionId + automatic + state.queueFilter;
    if (signature === state.questionSignature) return;
    state.questionSignature = signature;
    const list = $('question-list');
    const groups = {action:[], working:[], done:[]};
    for (const q of questions) groups[queueGroup(q)].push(q);
    for (const btn of $('queue-filters').querySelectorAll('button')) {
      const k = btn.dataset.queueFilter;
      text(btn.querySelector('span'), k === 'all' ? questions.length : groups[k].length);
      btn.setAttribute('aria-pressed', String(k === state.queueFilter));
    }
    if (!questions.length) { empty(list, '待查证的问题会出现在这里。'); return; }
    const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.questionId : null;
    const oldNodes = new Map([...list.querySelectorAll('[data-question-id]')].map((node) => [node.dataset.questionId, node]));
    const fragment = document.createDocumentFragment();
    for (const [group, items] of Object.entries(groups)) {
      if (!items.length || state.queueFilter !== 'all' && state.queueFilter !== group) continue;
      fragment.append(element('h3','queue-group-title',`${({action:'需要处理',working:'进行中',done:'已结束'})[group]} · ${items.length}`));
      for (const question of items) {
      let card = oldNodes.get(question.id);
      if (!card) {
        card = element('button', 'question-card');
        card.type = 'button';
        card.dataset.questionId = question.id;
        const meta = element('div', 'card-meta');
        meta.append(element('span', 'card-state-icon'), element('span', 'badge'), element('time'));
        const footer = element('div', 'card-footer');
        footer.append(element('span', 'card-stage'), element('span', 'priority'));
        card.append(meta, element('span', 'card-title'), footer);
      }
      card.setAttribute('aria-pressed', String(question.id === state.selectedQuestionId));
      const held = automationHeld(question);
      const status = stateGlyph(question);
      card.querySelector('.card-state-icon').replaceChildren(icon(status));
      card.dataset.signal = status;
      const evidence = !question.basis_stale && ['completed','insufficient'].includes(question.state) && evidenceStates[question.result?.evidence_status];
      text(card.querySelector('.badge'), question.basis_stale ? '依据待复核' : evidence || (held ? '等待恢复' : automatic && question.state === 'candidate' ? '等待研究' : researchLabel(question)));
      card.querySelector('.badge').dataset.state = held ? 'paused' : question.state || '';
      text(card.querySelector('time'), time(question.created_at));
      text(card.querySelector('.card-title'), question.title || '未命名问题');
      text(card.querySelector('.card-stage'), held ? '自动任务已暂停 · 可打开后立即查证' : question.state === 'backlog' ? '队列已满，等待执行' : ['running', 'failed', 'outcome_unknown', 'paused'].includes(question.state) ? question.error || stages[question.stage] || question.stage || '' : '');
      text(card.querySelector('.priority'), question.priority > 0 ? '优先' : '');
      fragment.append(card);
      }
    }
    if (fragment.childNodes.length) list.replaceChildren(fragment);
    else empty(list, '当前筛选下没有问题。');
    if (focusedId) [...list.querySelectorAll('[data-question-id]')].find((node) => node.dataset.questionId === focusedId)?.focus({preventScroll: true});
  }
  function questionFailureLabel(question) {
    return ['failed', 'outcome_unknown'].includes(question.state) && question.error_details?.label
      ? question.error_details.label : questionStates[question.state] || question.state || '状态未知';
  }
  function renderQuestionFailure(question) {
    const details = question.error_details || {};
    const error = question.error || (['failed', 'outcome_unknown'].includes(question.state) ? stages[question.stage] || question.stage : '');
    $('question-failure').hidden = !error;
    text($('question-failure-heading'), details.stage ? `${stages[details.stage] || details.stage}未完成` : '本次查证未完成');
    text($('question-error'), error || '');
    text($('question-error-suggestion'), details.suggestion || '可重试此问题；若持续失败，请检查服务设置和运行记录。');
    const note = question.state === 'outcome_unknown' ? '未能确认服务端是否已完成处理，应用不会自动重发此请求。' : '';
    text($('question-error-note'), note);
    $('question-error-note').hidden = !note;
    const facts = [];
    if (details.code) facts.push(['错误类型', details.code]);
    if (details.http_status) facts.push(['HTTP 状态', String(details.http_status)]);
    if (details.provider_code) facts.push(['服务错误码', details.provider_code]);
    const phases = {resolving: '解析服务域名', connecting: '建立连接', tls_handshake: 'HTTPS 安全握手', waiting_response: '等待服务响应', receiving: '接收响应内容', http_response: '服务返回错误'};
    if (phases[details.transport_phase]) facts.push(['失败环节', phases[details.transport_phase]]);
    if (Number.isFinite(details.timeout_seconds)) facts.push(['当时的等待时限', `${details.timeout_seconds} 秒`]);
    if (Number.isFinite(details.elapsed_ms)) facts.push(['实际耗时', `${(details.elapsed_ms / 1000).toFixed(1)} 秒`]);
    if (Number.isInteger(details.request_id)) facts.push(['本机请求记录', `${details.service === 'search' ? '搜索' : '模型'} #${details.request_id}`]);
    if (details.response?.max_tokens) facts.push(['当时的输出上限', `${details.response.max_tokens} token`]);
    if (Number.isInteger(details.response?.completion_tokens)) facts.push(['实际输出用量', `${details.response.completion_tokens} token`]);
    if (details.response?.finish_reason) facts.push(['模型结束原因', details.response.finish_reason]);
    if (details.provenance === 'historical_ledger') facts.push(['诊断依据', '本机历史请求账本与执行记录，未重新发送请求']);
    const list = $('question-error-facts');
    list.replaceChildren();
    for (const [name, value] of facts) list.append(element('dt', '', name), element('dd', '', value));
    $('question-error-details').hidden = !facts.length;
    if ($('question-failure').dataset.questionId !== question.id) $('question-error-details').open = false;
    $('question-failure').dataset.questionId = question.id;
  }
  function researchLabel(question) {
    if (question.state !== 'running') return questionFailureLabel(question);
    if (question.stage === 'searching') return '正在搜索网页';
    if (question.stage === 'reading') return '正在读取原文';
    const phase = question.progress?.model?.phase;
    return {queued: '等待模型执行', waiting: '等待模型响应', thinking: '正在思考', answering: '正在生成回答',
      validating: '正在核验回答与引用', correcting: '正在修正回答格式'}[phase] || stages[question.stage] || '正在查证';
  }
  function researchDuration(seconds) {
    const value = Math.max(0, Math.floor(seconds || 0));
    return value < 60 ? `${value} 秒` : `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
  }
  function renderResearchClock() {
    const question = selectedQuestion();
    const progress = question?.progress;
    if (!question || $('research-progress').hidden) return;
    const running = question.state === 'running';
    const now = (Date.now() - state.serverOffset) / 1000;
    const started = progress?.started_at || question.started_at;
    const end = running ? now : progress?.finished_at || question.updated_at;
    text($('research-elapsed'), started ? `${running ? '已用时' : '用时'} ${researchDuration(end - started)}` : '');
    const model = progress?.model || {};
    const since = model.last_response_at || model.request_started_at || progress?.stage_started_at || started || now;
    const quiet = Math.max(0, now - since);
    const syncLost = running && Date.now() - state.snapshotReceivedAt > 6000;
    const waiting = running && (quiet >= 15 || model.mode === 'buffered');
    $('research-wait').hidden = !syncLost && !waiting;
    text($('research-wait'), syncLost ? '进度同步已中断，正在重新连接；当前执行状态尚未更新。' : waiting && model.phase === 'queued' ? `正在等待模型执行位置，已等待 ${researchDuration(quiet)}。` : waiting ?
      `${model.mode === 'buffered' ? '此服务返回完整回答，暂时无法逐字显示。' : model.last_response_at ? `距上次响应 ${researchDuration(quiet)}。` : `尚未收到${question.stage === 'searching' ? '搜索' : '模型'}内容，已等待 ${researchDuration(quiet)}。`}${model.timeout_seconds && model.request_started_at ? `本次模型请求超时设置为 ${model.timeout_seconds} 秒。` : ''}` : '');
  }
  function renderResearchProgress(question) {
    const p = question.progress;
    const panel = $('research-progress');
    panel.hidden = !(p && p.version === question.version) && question.state !== 'running';
    if (panel.hidden) return;
    const running = question.state === 'running';
    const key = `${question.id}:${question.generation}:${p?.started_at || question.started_at || ''}`;
    if (panel.dataset.runKey !== key) {
      panel.dataset.runKey = key;
      $('research-process').open = running;
      $('research-thinking').open = false;
      $('research-events').dataset.signature = '';
      $('research-source-list').dataset.signature = '';
    }
    if (panel.dataset.running === 'true' && !running) $('research-process').open = false;
    panel.dataset.running = String(running);
    text($('research-status-title'), running ? researchLabel(question) : ['completed', 'insufficient'].includes(question.state) ? '查证已完成' : question.state === 'cancelled' ? '查证已取消' : question.state === 'paused' ? '查证已暂停' : '查证已停止');
    const query = p?.query || (running ? question.public_query : '');
    $('research-query').hidden = !query;
    text($('research-query'), query ? `搜索：${query}` : '');
    const sourceItems = p?.sources || [];
    text($('research-source-count'), sourceItems.length ? `已读取 ${sourceItems.length} 份原文` : '');
    const eventLabels = {searching: '开始搜索网页', search_query: '检索词已准备', reading: '正在检查搜索结果中的原文',
      sources_ready: '已读取原文', synthesizing: '准备结合资料回答', model_queued: '已进入模型队列',
      model_request: '请求已发送，等待响应', model_validating: '核验回答结构和原文引用', model_correcting: '正在修正回答格式与引用'};
    const events = (p?.events || []).filter(event => !['search_query', 'reading', 'synthesizing', 'model_queued'].includes(event.kind));
    const eventList = $('research-events');
    const eventSignature = JSON.stringify(events);
    if (eventList.dataset.signature !== eventSignature) {
      eventList.dataset.signature = eventSignature;
      eventList.replaceChildren();
      for (const event of events) {
        const row = element('li');
        row.append(element('time', '', time(event.at)), element('span', '', `${eventLabels[event.kind] || stages[event.kind] || '更新进度'}${event.kind === 'sources_ready' ? ` · ${event.count} 份` : ''}`));
        eventList.append(row);
      }
    }
    const sourcesNode = $('research-source-list');
    const sourceSignature = JSON.stringify(sourceItems);
    if (sourcesNode.dataset.signature !== sourceSignature) {
      sourcesNode.dataset.signature = sourceSignature;
      sourcesNode.replaceChildren();
      for (const source of sourceItems) {
        const item = element('article', 'research-source-item');
        const url = safeURL(source.url);
        item.append(sourceLink(source, source.title || url?.hostname || '已读取的来源'));
        if (url) item.append(element('p', 'research-source-domain', url.hostname));
        if (source.excerpt) item.append(element('p', '', source.excerpt));
        sourcesNode.append(item);
      }
    }
    const model = p?.model;
    $('research-thinking').hidden = !model;
    if (model) {
      text($('research-thinking-title'), running && model.phase === 'thinking' ? '正在思考' : running && model.phase === 'waiting' ? '等待模型响应' : '模型处理');
      text($('research-model-detail'), [model.request_started_at ? `请求发出于 ${time(model.request_started_at)}` : '正在等待执行位置',
        model.last_response_at ? `最近响应于 ${time(model.last_response_at)}` : '尚未收到响应',
        model.reasoning_chars ? `已收到推理响应 ${model.reasoning_chars} 字` : '',
        model.answer_chars ? `已收到回答 ${model.answer_chars} 字` : '',
        model.attempt > 1 ? '正在进行一次格式校正' : ''].filter(Boolean).join('\n'));
    }
    const draft = running && model?.draft;
    $('research-draft').hidden = !draft;
    text($('research-draft-text'), draft || '');
    renderResearchClock();
  }
  function renderDetail() {
    const question = selectedQuestion();
    $('result-empty').hidden = Boolean(question);
    $('result-content').hidden = !question;
    if (!question) { text($('result-version'), ''); state.detailSignature = ''; return; }
    renderResearchProgress(question);
    if (question.state === 'running') text($('question-state'), researchLabel(question));
    const aid = artifactID(question);
    const docRecord = questionDocument(question);
    const automatic = Boolean(state.snapshot?.meeting?.permissions?.auto_search);
    const held = automationHeld(question);
    const signature = JSON.stringify({...question, progress: undefined}) + JSON.stringify(question.progress?.sources) + JSON.stringify(docRecord) + state.snapshot?.meeting?.state + automatic + held;
    if (signature === state.detailSignature) return;
    state.detailSignature = signature;
    const body = document.querySelector('.detail-body');
    const scrollTop = body.scrollTop;
    text($('result-version'), `版本 ${question.version || 1}`);
    text($('question-title'), question.title || '未命名问题');
    text($('question-state'), held ? '等待恢复' : automatic && question.state === 'candidate' ? '等待研究' : researchLabel(question));
    $('question-state').dataset.state = held ? 'paused' : question.state || '';
    const evidenceState = question.state === 'running' ? '' : question.result?.evidence_status;
    text($('evidence-state'), question.basis_stale ? '依据已更正·结果待复核' : evidenceStates[evidenceState] || evidenceState || '尚未形成证据');
    $('evidence-state').dataset.evidence = question.basis_stale ? 'insufficient' : evidenceState || '';
    const basis = (question.source_refs || []).map((ref) => {
      const segment = (state.snapshot?.segments || []).find((item) => item.segment_id === ref.segment_id);
      if (!segment) return `片段 ${ref.segment_id}（修订 ${ref.revision}）`;
      if (retracted(segment)) return `${time(segmentTime(segment))} 发言已撤回`;
      if (segment.revision !== ref.revision) return `${time(segmentTime(segment))} 原修订 ${ref.revision} 已更正`;
      return `${time(segmentTime(segment))}「${segment.text}」`;
    });
    const origin = basis.length ? `发言依据：${basis.join('；')}` : question.origin === 'manual' ? '来源：手动研究问题' : '';
    text($('question-trigger'), [origin, question.public_query ? `公开检索词：${question.public_query}` : ''].filter(Boolean).join('\n'));
    $('locate-question').hidden = question.basis_stale || question.state === 'superseded' || !(question.anchor_ref || question.source_refs?.[0])?.segment_id;
    renderQuestionFailure(question);
    const research = researchAvailable();
    const questionMenu = document.querySelector('.question-menu');
    const needsAction = research && (held || (question.state === 'candidate' ? !automatic : ['needs_review', 'failed', 'outcome_unknown', 'backlog', 'paused'].includes(question.state)));
    if (questionMenu.dataset.questionId !== question.id || needsAction) questionMenu.open = needsAction;
    questionMenu.dataset.questionId = question.id;
    $('approve-question').hidden = question.state !== 'candidate' || automatic || !research;
    $('edit-question').disabled = !research || question.state === 'superseded';
    text($('edit-question'), question.state === 'needs_review' ? '确认更正后的范围' : question.state === 'backlog' ? '整理问题范围' : '修改范围');
    $('set-public-query').hidden = !research || state.snapshot?.meeting?.data_scope !== 'real' || question.basis_stale || ['running', 'cancelled', 'superseded'].includes(question.state);
    $('prioritize-question').hidden = !['queued', 'paused'].includes(question.state) || !research;
    $('retry-question').hidden = !(held || retryStates.has(question.state)) || !research;
    text($('retry-question'), held ? '立即查证' : question.state === 'paused' ? '继续查证' : '重试此问题');
    $('cancel-question').hidden = !activeStates.has(question.state) || !research;
    for (const action of ['approve', 'prioritize', 'retry', 'cancel']) $(`${action}-question`).disabled = state.pendingButtons.has(`${action}-question`);
    const result = question.state === 'running' ? {} : question.result || {};
    const sourceItems = Array.isArray(result.sources) ? result.sources : question.progress?.sources || [];
    renderAnswer(result, question, sourceItems, automatic);
    renderClaims(result.claims || [], sourceItems);
    renderUncertainties(result.uncertainty || [], Boolean(result.brief));
    renderSources(sourceItems);
    text($('source-count'), sourceItems.length);
    $('download-document').disabled = !aid;
    text($('document-path'), '');
    text($('document-state'), aid ? '文档已登记' : '尚未生成文档');
    text($('document-preview'), aid ? '切换到 Markdown 查看文档。' : '文档生成后可在这里预览。');
    body.scrollTop = scrollTop;
    setDetailTab(state.detailTab);
  }
  function answerParagraphs(container, value) {
    for (const paragraph of value.split(/\n\s*\n/).filter(part => part.trim())) container.append(element('p', 'answer-text', paragraph));
  }
  async function copyAnswer(value) {
    if (navigator.clipboard?.writeText) {
      try { await navigator.clipboard.writeText(value); return; } catch { /* WebKit may require the selection-based path. */ }
    }
    const previous = document.activeElement;
    const selection = window.getSelection();
    const ranges = selection ? Array.from({length: selection.rangeCount}, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
    const field = element('textarea', 'clipboard-field');
    field.value = value;
    field.setAttribute('aria-label', '复制回答');
    document.body.append(field);
    try {
      field.focus(); field.select();
      if (!document.execCommand('copy')) throw new Error('复制未完成，请展开 Markdown 后手动复制。');
    } finally {
      field.remove();
      previous?.focus({preventScroll: true});
      if (selection) { selection.removeAllRanges(); for (const range of ranges) selection.addRange(range); }
    }
  }
  function renderAnswer(result, question, sourceItems, automatic) {
    const container = $('answer-content');
    container.replaceChildren();
    if (question.state === 'running' && !$('research-progress').hidden) return;
    const brief = result.brief;
    if (!brief) {
      if (result.answer) {
        answerParagraphs(container, result.answer);
        if (result.original_evidence) { const proof = element('section', 'original-evidence'); renderOriginalEvidence(proof, result.original_evidence); container.append(proof); }
        return;
      }
      container.append(element('p', 'muted', automationHeld(question) ? '自动查证已暂停。点击“立即查证”只处理这个问题，也可以恢复查证后继续。' : question.state === 'paused' ? '查证已暂停。点击“继续查证”处理这个问题，或在上方恢复未完成的查证。' : question.state === 'candidate' ? automatic ? '问题已识别，将自动排队查证。' : '本场已关闭自动查证，可在需要时开始查证。' : question.state === 'needs_review' ? '发言依据已更正或撤回，请核对最新转写并确认问题范围。' : question.state === 'backlog' ? '已加入查证队列，空出执行位置后会自动开始。' : question.state === 'running' ? `${stages[question.stage] || '研究正在进行'}，完成后在这里呈现参考回答。` : question.state === 'queued' ? question.stage || '已进入研究队列，等待执行。' : '尚无参考回答。'));
      return;
    }
    const briefSection = element('section', 'answer-brief');
    briefSection.setAttribute('aria-label', '一句话结论');
    const heading = element('div', 'brief-heading');
    const copy = element('button', 'text-button copy-brief', '复制回答');
    heading.append(element('h3', '', '一句话结论'), copy);
    briefSection.append(heading, element('p', 'short-answer', brief.short_answer));
    container.append(briefSection);
    if (result.answer) {
      const analysis = element('section', 'answer-analysis');
      analysis.setAttribute('aria-label', '完整分析');
      analysis.append(element('h3', '', '完整分析'));
      answerParagraphs(analysis, result.answer);
      container.append(analysis);
    }
    if (result.original_evidence) {
      const proof = element('section', 'original-evidence');
      renderOriginalEvidence(proof, result.original_evidence);
      container.append(proof);
    }
    const evidence = element('details', 'answer-disclosure');
    evidence.append(element('summary', '', '适用范围与关键依据'));
    const points = element('ul', 'brief-points');
    const kinds = {fact: '资料事实', source_claim: '来源主张', inference: '模型推断', explanation: '参考说明'};
    for (const point of brief.key_points || []) {
      const item = element('li');
      item.append(element('span', 'point-kind', kinds[point.kind] || '参考说明'), element('span', 'point-text', point.text));
      const refs = element('span', 'brief-references');
      const sourceIds = new Set((point.claim_indices || []).map(index => result.claims?.[index - 1]?.source_id).filter(Boolean));
      for (const id of sourceIds) {
        const source = sourceItems.find(entry => entry.id === id);
        if (source) refs.append(sourceLink(source, `来源 ${sourceItems.indexOf(source) + 1}`));
      }
      if (refs.childElementCount) item.append(refs);
      points.append(item);
    }
    if (brief.caveat) {
      const caveat = element('p', 'brief-caveat');
      caveat.append(element('strong', '', '适用限制'), document.createTextNode(brief.caveat));
      evidence.append(caveat);
    }
    evidence.append(points);
    container.append(evidence);
    copy.addEventListener('click', async () => {
      copy.disabled = true;
      const value = [question.title, evidenceStates[result.evidence_status] || '',
        ...(question.basis_stale ? ['依据已更正，当前回答待复核。'] : []), '', '一句话结论', brief.short_answer, '', '完整分析', result.answer || '', '', `适用限制：${brief.caveat}`, '',
        ...(result.original_evidence ? [result.original_evidence.label, result.original_evidence.quote || result.original_evidence.message,
          safeURL(result.original_evidence.url)?.href || '', ''] : []),
        ...(brief.key_points || []).map(point => `- 【${kinds[point.kind] || '参考说明'}】${point.text}`),
        '',
        ...sourceItems.map((source, i) => `来源 ${i + 1}：${source.title}\n${safeURL(source.url)?.href || ''}`)].join('\n');
      try { await copyAnswer(value); notice('结论、完整分析和来源已复制。'); }
      catch (error) { notice(error.message || '复制失败，请手动复制 Markdown。', true); }
      finally { copy.disabled = false; }
    });
  }
  function renderClaims(claims, sourceItems) {
    const container = $('claim-content');
    container.replaceChildren();
    if (!Array.isArray(claims) || !claims.length) return;
    const details = element('details', 'answer-disclosure');
    details.append(element('summary', '', `原文证据 · ${claims.length} 条`));
    const list = element('ul', 'claim-list');
    const kinds = {fact: '资料事实', inference: '模型推断', explanation: '参考解释', source_claim: '来源主张', supported: '来源支持'};
    for (const claim of claims) {
      const item = element('li');
      if (claim.kind) item.append(element('span', 'claim-kind', kinds[claim.kind] || claim.kind));
      item.append(document.createTextNode(readable(claim.text || claim)));
      if (claim.quote) item.append(element('blockquote', 'source-excerpt', claim.quote));
      const refs = element('span', 'claim-references');
      for (const id of claim.source_id ? [claim.source_id] : []) {
        const source = sourceItems.find((entry) => entry.id === id);
        if (source) refs.append(sourceLink(source, `来源 ${sourceItems.indexOf(source) + 1}`));
      }
      if (refs.childElementCount) item.append(refs);
      list.append(item);
    }
    details.append(list); container.append(details);
  }
  function renderUncertainties(uncertainties, hasBrief) {
    const container = $('uncertainty-content');
    container.replaceChildren();
    if (!Array.isArray(uncertainties) || !uncertainties.length) return;
    const list = element('ul', 'uncertainty-list');
    for (const item of uncertainties) list.append(element('li', '', readable(item)));
    const details = element('details', 'answer-disclosure');
    details.open = !hasBrief;
    details.append(element('summary', '', hasBrief ? '其他分歧与限制' : '分歧与限制'), list);
    container.append(details);
  }
  function renderSources(sourceItems) {
    const container = $('source-list');
    container.replaceChildren();
    if (!sourceItems.length) { empty(container, '尚无可核验来源。'); return; }
    for (let index = 0; index < sourceItems.length; index += 1) {
      const source = sourceItems[index];
      const item = element('article', 'source-item');
      const title = element('div', 'source-title');
      title.append(sourceLink(source, `${index + 1}. ${source.title || '未命名来源'}`));
      if (source.fixture) title.append(element('span', 'fixture-label', '合成资料'));
      const url = safeURL(source.url);
      item.append(title, element('span', 'source-url', url ? url.href : '未提供可打开的 HTTP(S) 原文地址'));
      item.append(element('p', 'source-meta', `${source.read_status === 'metadata_only' ? '仅检索到题录或链接' : `读取时间：${time(source.retrieved_at, true)}`}${source.published_at ? ` · 发布或更新时间：${source.published_at}` : ''}`));
      if (source.excerpt) item.append(element('p', 'source-excerpt', source.excerpt));
      container.append(item);
    }
  }
  function renderActivity(activity) {
    activity = activity.filter((event) => !['transcript.interim', 'audio.level'].includes(event.type));
    const signature = JSON.stringify(activity);
    if (signature === state.activitySignature) return;
    state.activitySignature = signature;
    const list = $('activity-list');
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 35;
    const scrollTop = list.scrollTop;
    list.replaceChildren();
    if (!activity.length) { list.append(element('li', 'muted', '暂无事件')); return; }
    for (const event of activity) {
      const item = element('li', 'activity-item');
      item.append(element('time', '', time(event.received_at)), element('span', '', activityMessage(event)));
      list.append(item);
    }
    list.scrollTop = atBottom ? list.scrollHeight : scrollTop;
  }
  function activityMessage(event) {
    const labels = {'meeting.created': '会议已创建', 'meeting.end': '会议已结束', 'meeting.pause_research': '自动查证已暂停，已开始的任务继续', 'meeting.resume_research': '自动查证已恢复，接着未处理的转写继续', 'meeting.retry_coordinator': '已单独重试问题识别', 'question.created': '研究问题已建立', 'question.revised': '问题范围已更新', 'question.cancel': '任务已取消', 'question.retry': '问题已重新加入队列', 'question.prioritize': '问题已设为优先处理', 'question.approve': '问题已确认查证', 'research.started': '研究已开始', 'research.searching': '正在搜索资料', 'research.reading': '正在读取原文', 'research.synthesizing': '正在整理参考回答', 'research.completed': '研究结束', 'research.failed': '研究请求失败', 'document.saved': 'Markdown 已保存', 'document.conflict': '文档保存冲突，保留人工修改', 'document.save_failed': '文档未能写入目录', 'transcript.final': '收到一段转写', 'transcript.retracted': '转写片段已撤回', 'audio.ready': '采音组件已就绪', 'audio.paused': '采音已暂停', 'audio.stopped': '采音已停止', 'audio.gap': '采音存在缺口', 'audio.error': '采音异常', 'audio.status': '采音状态已更新', 'audio.model_status': '转写模型状态已更新', 'model.status': '转写模型状态已更新', 'coordinator.processed': '已分析新的讨论内容', 'coordinator.failed': '问题识别请求未完成'};
    const payload = event.payload || {};
    if (event.type === 'coordinator.retry_scheduled') return `问题识别将在 ${payload.delay_seconds} 秒后自动重试（${payload.attempts}/${payload.max_attempts}）`;
    if (event.type === 'coordinator.retrying') return `正在自动重试问题识别（${payload.attempt}/${payload.max_attempts}）`;
    const question = (state.snapshot?.questions || []).find((item) => item.id === payload.question_id);
    const suffix = question ? ` · ${question.title.slice(0, 50)}${question.title.length > 50 ? '…' : ''}` : payload.message ? ` · ${payload.message}` : '';
    const diagnostic = payload.error_details;
    const reason = diagnostic?.message || diagnostic?.label;
    return `${labels[event.type] || event.type || '事件'}${suffix}${reason ? ` · ${reason}` : payload.code ? `（${payload.code}）` : ''}`;
  }
  function revealAnswer() {
    requestAnimationFrame(() => {
      if (state.route === 'meeting' && !wideMeeting() && $('work-grid').dataset.mobilePanel === 'results') $('results-panel').scrollIntoView({block: 'nearest', behavior: 'instant'});
    });
  }
  function wideMeeting() { return window.matchMedia('(min-width:1101px)').matches; }
  function highlightQuestionNote() {
    const selected = $('work-grid').dataset.mobilePanel === 'results' && state.selectedQuestionId;
    for (const note of $('transcript-list').querySelectorAll('[data-note-id]')) note.classList.toggle('match', note.dataset.noteId === selected);
  }
  function transcriptVisible() {
    return state.route === 'meeting' && $('work-grid').dataset.leftPane === 'transcript' && (wideMeeting() || $('work-grid').dataset.mobilePanel === 'transcript');
  }
  function transcriptViewport() {
    const list = $('transcript-list');
    return /auto|scroll/.test(getComputedStyle(list).overflowY) && list.clientHeight > 0 ? list : $('main');
  }
  function setMeetingTab(panel, updateURL = true) {
    const grid = $('work-grid');
    if (grid.dataset.leftPane !== panel && ['summary','files','transcript'].includes(panel)) filePreviewRequest++;
    grid.dataset.mobilePanel = panel;
    highlightQuestionNote();
    grid.dataset.meetingTab = panel === 'results' ? 'questions' : panel;
    if (['transcript','files','summary'].includes(panel)) grid.dataset.leftPane = panel;
    $('results-panel').hidden = panel !== 'results';
    $('questions-panel').hidden = panel === 'results';
    if (panel !== 'results') state.returnNote = null;
    text($('back-questions-label'), state.returnNote ? '返回发言' : '全部问题');
    $('meeting-files-panel').hidden = grid.dataset.leftPane === 'transcript';
    text($('meeting-files-heading'), grid.dataset.leftPane === 'summary' ? '会议纪要' : '会议文件');
    if (panel === 'summary') {
      const oldPreview = $('meeting-file-title').textContent;
      if (oldPreview !== '会议纪要') $('meeting-file-preview').hidden = true;
      if (state.snapshot?.meeting?.base_files?.summary?.state === 'saved') previewMeetingFile(state.meetingId, 'summary');
    }
    document.querySelectorAll('button[data-meeting-tab]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.meetingTab === (panel === 'results' ? 'questions' : panel))));
    if (updateURL && state.route === 'meeting' && state.meetingId) history.replaceState(null, '', meetingHash());
    if (panel === 'results') revealAnswer();
  }
  function setDetailTab(tab, fresh = false) {
    state.detailTab = tab;
    for (const name of ['answer', 'sources', 'document']) $(`${name}-panel`).hidden = name !== tab;
    document.querySelectorAll('[data-detail]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.detail === tab)));
    if (tab === 'document') previewArtifact(fresh);
    if (state.route === 'meeting' && $('work-grid').dataset.mobilePanel === 'results') history.replaceState(null, '', meetingHash());
  }
  async function getArtifact(question, fresh = false) {
    const aid = artifactID(question);
    if (!aid) return null;
    const doc = questionDocument(question);
    const key = `${aid}:${question.version || 1}:${doc?.state || ''}:${doc?.hash || ''}:${Boolean(doc?.basis_stale)}`;
    if (!fresh && state.artifactCache.has(key)) return state.artifactCache.get(key);
    if (!state.artifactRequests.has(key)) {
      const token = state.token;
      const pending = api(`/live/v2/artifacts/${encodeURIComponent(aid)}`)
        .then((result) => {
          const artifact = result.document ? {...result.document, content: result.content} : result;
          if (state.token === token) state.artifactCache.set(key, artifact);
          return artifact;
        })
        .finally(() => state.artifactRequests.delete(key));
      state.artifactRequests.set(key, pending);
    }
    return state.artifactRequests.get(key);
  }
  async function previewArtifact(fresh = false) {
    const question = selectedQuestion();
    if (!question || !artifactID(question)) return;
    const selection = state.selection;
    const id = question.id;
    const version = question.version;
    text($('document-state'), '正在读取文档');
    try {
      const artifact = await getArtifact(question, fresh);
      if (selection !== state.selection || selectedQuestion()?.id !== id || selectedQuestion()?.version !== version) return;
      const documentStates = {saved: '已保存到会议目录', conflict: '保存冲突：已保留人工修改', save_failed: '保存失败：可先下载；恢复目录写入权限后重启应用，会重试保存', pending: '文档待写入目录', superseded: '任务状态已变化，此版本未发布到目录'};
      const documentState = artifact.basis_stale ? '依据或范围已更正：当前文档仅供历史参考' : artifact.externally_modified ? '文件已有人工修改，显示磁盘中的内容' : documentStates[artifact.state] || artifact.state || '文档状态未知';
      text($('document-state'), artifact.on_disk === false && artifact.state === 'saved' ? '磁盘文件不存在，显示已登记内容' : documentState);
      $('document-state').dataset.state = artifact.state || '';
      text($('document-path'), artifact.absolute_path || artifact.path || '');
      text($('document-preview'), artifact.content || '文档内容为空。');
    } catch (error) {
      if (selection === state.selection && selectedQuestion()?.id === id && selectedQuestion()?.version === version) {
        text($('document-state'), '未能读取文档');
        text($('document-preview'), error.message);
      }
    }
  }
  async function perform(button, action) {
    if (button.disabled || state.pendingButtons.has(button.id)) return;
    state.pendingButtons.add(button.id);
    button.disabled = true;
    try { await action(); }
    catch (error) { notice(error.status === 409 ? `问题或会议已更新，请查看最新状态后重试。${error.message}` : error.message, true); }
    finally { state.pendingButtons.delete(button.id); button.disabled = false; if (state.snapshot) { state.detailSignature = ''; renderSnapshot(); } }
  }
  async function refreshNow() {
    if (state.request) {
      state.request.controller.abort();
      state.request = null;
    }
    await refresh();
  }
  async function questionAction(action, button) {
    const question = selectedQuestion();
    if (!question) return;
    const selection = state.selection;
    await perform(button, async () => {
      await api(`/live/v2/questions/${encodeURIComponent(question.id)}/actions`, {method: 'POST', body: {expected_version: question.version, action}});
      if (selection === state.selection) await refreshNow();
    });
  }
  function openQueryDialog() {
    const question = selectedQuestion();
    if (!question || question.basis_stale) return;
    state.queryQuestion = {id: question.id, version: question.version, meetingId: state.meetingId};
    $('public-query-input').value = question.public_query || '';
    text($('search-query-context'), `问题：${question.title}`);
    text($('search-query-error'), '');
    $('search-query-dialog').showModal();
    $('public-query-input').focus();
  }
  async function meetingControl(action, button) {
    const id = state.meetingId;
    if (!id) return;
    await perform(button, async () => {
      await api(`/live/v2/meetings/${encodeURIComponent(id)}/controls`, {method: 'POST', body: {action}});
      if (id === state.meetingId) await refreshNow();
    });
  }
  function openMeetingDialog() {
    if (state.native?.meetingId) {
      notice('当前会议正在收听。请先暂停收听，再开始另一场会议。');
      selectMeeting(state.native.meetingId);
      return;
    }
    $('meeting-form').reset();
    $('meeting-research').checked = Boolean(state.home?.capabilities?.model_enabled && state.home?.capabilities?.search_enabled);
    $('meeting-audio-source').value = state.native?.audioSource || 'system';
    $('meeting-scope').querySelector('[value="synthetic"]').disabled = state.home?.project?.data_scope !== 'synthetic';
    updateMeetingForm();
    text($('meeting-error'), '');
    $('meeting-dialog').showModal();
    $('meeting-title').focus();
  }
  function updateMeetingForm() {
    const synthetic = $('meeting-scope').value === 'synthetic';
    const desktop = nativeAvailable();
    text($('meeting-dialog-intro'), synthetic ? '用一段示例讨论，体验转写、查证和资料保存。无需录音或联网。' : desktop ? '选择要听的声音，实时转写并整理查证资料。' : '先创建会议。请在桌面 App 中收听，浏览器可手动补充讨论。');
    text($('meeting-submit'), synthetic ? '创建并体验示例' : desktop && state.native?.modelState === 'installed' ? '创建并开始收听' : '创建会议');
    $('meeting-audio-help').hidden = synthetic;
    $('meeting-source-field').hidden = synthetic || !desktop;
    $('meeting-audio-source').disabled = Boolean(state.native?.meetingId) || state.native?.canSelectSource === false;
    const input = audioInputs[$('meeting-audio-source').value] || audioInputs.system;
    text($('meeting-audio-help'), desktop && state.native?.modelState === 'downloadable' ? '首次收听需要安装中文模型。创建后可前往设置安装。' : input.help);
    $('meeting-setup-warning').hidden = synthetic || Boolean(state.home?.capabilities?.model_enabled && state.home?.capabilities?.search_enabled);
    $('meeting-permissions-field').hidden = synthetic;
    $('meeting-research').disabled = !state.home?.capabilities?.model_enabled || !state.home?.capabilities?.search_enabled;
    text($('meeting-research-help'), `音频留在本机；取消勾选可仅做本机转写。${requestLimitsText()}`);
  }
  async function playDemo(id) {
    await api(`/live/v2/meetings/${encodeURIComponent(id)}/demo`, {method:'POST', body:{}});
    if (id === state.meetingId) await refreshNow();
  }
  function updateImportForm() {
    const native = state.native;
    const file = native?.audioFile;
    const caps = state.home?.capabilities || {};
    if (file && state.importFileID !== file.id) {
      state.importFileID = file.id;
      if (!state.importTarget) $('import-title').value = file.name.replace(/\.[^.]+$/, '').slice(0,120);
    }
    text($('audio-file-name'), file?.name || '尚未选择音频');
    text($('audio-file-meta'), file ? `${audioTime(file.durationMS)} · ${(file.bytes / 1024 / 1024).toFixed(1)} MB` : 'WAV、M4A、MP3、AIFF、CAF、FLAC');
    text($('import-desktop-help'), !nativeAvailable() ? '请在桌面 App 中导入音频。浏览器可回看已导入的会议。' : native?.fileModelState === 'downloading' ? '中文模型正在安装，完成后即可导入。' : native?.fileModelState !== 'installed' ? '首次文件转写需要安装中文识别模型，点击下方按钮安装。' : '音频仅在本机处理，无需播放。支持不超过 2 GB、六小时的录音。');
    $('import-install-model').hidden = !nativeAvailable() || native?.fileModelState !== 'downloadable';
    $('import-install-model').disabled = !native?.ready || Boolean(native?.meetingId);
    $('choose-audio').disabled = !nativeAvailable() || !native?.ready || Boolean(native.meetingId);
    $('import-model').disabled = !caps.model_enabled;
    $('import-search').disabled = !caps.search_enabled || !$('import-model').checked;
    if ($('import-model').disabled) $('import-model').checked = false;
    if ($('import-search').disabled) $('import-search').checked = false;
    text($('import-services-help'), caps.model_enabled && caps.search_enabled ? `默认开启，取消勾选可仅做本机转写。${requestLimitsText()}` : '联网服务尚未就绪。可以先导入并本机转写，之后在设置中连接模型与搜索。');
    $('import-submit').disabled = !file || !native?.canImport || state.pendingButtons.has('import-submit');
    const fileError = native?.audioFileError || '';
    if (state.nativeFileError !== fileError) {
      state.nativeFileError = fileError;
      text($('import-error'), fileError);
    }
  }
  function openImportDialog(target = '') {
    if (state.native?.meetingId) { notice('请先停止当前收听或导入，再导入另一份音频。'); return; }
    state.importTarget = target;
    state.importFileID = '';
    $('import-title').value = target ? state.snapshot?.meeting?.title || '' : '';
    $('import-model').checked = Boolean(state.home?.capabilities?.model_enabled);
    $('import-search').checked = Boolean(state.home?.capabilities?.model_enabled && state.home?.capabilities?.search_enabled);
    text($('import-error'), ''); updateImportForm();
    $('import-dialog').showModal();
  }
  function downloadMarkdown(content, filename) {
    const url = URL.createObjectURL(new Blob([content], {type:'text/markdown;charset=utf-8'}));
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename;
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  for (const id of ['import-audio', 'library-import-audio', 'guide-import']) $(id).addEventListener('click', () => openImportDialog());
  $('choose-audio').addEventListener('click', () => { text($('import-error'), ''); nativePost('chooseAudioFile'); });
  $('import-install-model').addEventListener('click', () => nativePost('installModel'));
  $('import-model').addEventListener('change', updateImportForm);
  $('restart-import').addEventListener('click', () => openImportDialog(state.snapshot?.meeting?.audio_import ? '' : state.meetingId));
  $('import-form').addEventListener('submit', async event => {
    event.preventDefault();
    const file = state.native?.audioFile;
    const title = $('import-title').value.trim();
    if (!title || !file || $('import-submit').disabled) return;
    state.pendingButtons.add('import-submit'); updateImportForm();
    text($('import-error'), '');
    let id = state.importTarget;
    try {
      if (!id) {
        const meeting = await api('/live/v2/meetings', {method:'POST', body:{title, data_scope:'real', input_kind:'audio_file',
          permissions:{model:$('import-model').checked, search:$('import-search').checked, auto_search:$('import-search').checked}}});
        id = meeting.id; state.importTarget = id;
        state.home.meetings.unshift(meeting); renderMeetingOptions();
      }
      else await api(`/live/v2/meetings/${id}/permissions`, {method:'POST', body:{model:$('import-model').checked, search:$('import-search').checked, auto_search:$('import-search').checked}});
      await selectMeeting(id);
      $('import-dialog').close();
      nativePost('importAudioFile', id, file.id);
      notice('已建立音频会议，正在准备本机转写。');
      await refreshHome();
    } catch (error) { text($('import-error'), error.message); }
    finally { state.pendingButtons.delete('import-submit'); updateImportForm(); }
  });
  $('download-transcript').addEventListener('click', event => perform(event.currentTarget, async () => {
    const value = await api(`/live/v2/meetings/${encodeURIComponent(state.meetingId)}/transcript`);
    downloadMarkdown(value.content, value.filename);
  }));
  $('close-file-preview').addEventListener('click', () => { filePreviewRequest++; $('meeting-file-preview').hidden = true; });
  $('authorize-research').addEventListener('click', () => {
    state.permissionTarget = state.meetingId;
    $('permissions-model').checked = Boolean(state.snapshot?.meeting?.permissions?.model);
    $('permissions-model').disabled = !state.home?.capabilities?.model_enabled;
    $('permissions-search').checked = Boolean(state.snapshot?.meeting?.permissions?.search);
    $('permissions-auto-search').checked = Boolean(state.snapshot?.meeting?.permissions?.auto_search);
    $('permissions-search').disabled = !state.home?.capabilities?.search_enabled || !$('permissions-model').checked;
    $('permissions-auto-search').disabled = $('permissions-search').disabled || !$('permissions-search').checked;
    text($('permissions-error'), ''); $('permissions-dialog').showModal();
  });
  $('permissions-model').addEventListener('change', () => {
    $('permissions-search').disabled = !state.home?.capabilities?.search_enabled || !$('permissions-model').checked;
    if ($('permissions-search').disabled) $('permissions-search').checked = false;
    updateAutoSearchPermission();
  });
  function updateAutoSearchPermission() {
    $('permissions-auto-search').disabled = $('permissions-search').disabled || !$('permissions-search').checked;
    if ($('permissions-auto-search').disabled) $('permissions-auto-search').checked = false;
  }
  $('permissions-search').addEventListener('change', updateAutoSearchPermission);
  $('permissions-form').addEventListener('submit', async event => {
    event.preventDefault(); const id = state.permissionTarget;
    if (!id || $('permissions-submit').disabled) return;
    $('permissions-submit').disabled = true;
    try {
      await api(`/live/v2/meetings/${id}/permissions`, {method:'POST', body:{model:$('permissions-model').checked, search:$('permissions-search').checked, auto_search:$('permissions-auto-search').checked}});
      $('permissions-dialog').close(); if (id === state.meetingId) await refreshNow();
      notice($('permissions-auto-search').checked ? '本场已允许自动识别问题、查证公开资料。' : '本场授权已保存。搜索前请逐条确认公开检索词。');
    } catch (error) { text($('permissions-error'), error.message); }
    finally { $('permissions-submit').disabled = false; }
  });
  async function tryDemo(button) {
    if (state.native?.meetingId) { notice('请先暂停当前会议的收听，再体验示例。'); return; }
    await perform(button, async () => {
      let meeting = (state.home?.meetings || []).find(item => item.state === 'active' && item.data_scope === 'synthetic' && item.title === '示例会议 · 工作流查证');
      if (!meeting) {
        meeting = await api('/live/v2/meetings', {method:'POST', body:{title:'示例会议 · 工作流查证', data_scope:'synthetic'}});
        state.home.meetings.unshift(meeting); renderMeetingOptions();
      }
      await selectMeeting(meeting.id);
      if (!(state.snapshot?.segments?.length || state.snapshot?.questions?.length)) await playDemo(meeting.id);
      await refreshHome();
      notice('示例已开始。先看转写，再打开“查证问题”查看生成的资料。');
    });
  }

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const token = $('access-code').value.trim();
    if (!token || $('login-submit').disabled) return;
    $('login-submit').disabled = true;
    text($('login-error'), '');
    try { await login(token); }
    catch (error) { text($('login-error'), error.message); state.token = ''; setSync('未能连接', true); }
    finally { $('login-submit').disabled = false; }
  });
  $('meeting-select').addEventListener('change', (event) => selectMeeting(event.target.value));
  for (const id of ['new-meeting', 'library-new-meeting', 'empty-create', 'guide-start']) $(id).addEventListener('click', openMeetingDialog);
  for (const id of ['try-demo', 'guide-demo']) $(id).addEventListener('click', (event) => tryDemo(event.currentTarget));
  $('meeting-scope').addEventListener('change', updateMeetingForm);
  for (const id of ['meeting-audio-source', 'capture-source']) $(id).addEventListener('change', event => {
    nativePost('setAudioSource', null, null, event.target.value);
  });
  $('open-microphone-settings').addEventListener('click', () => nativePost('openMicrophoneSettings'));
  const compactRail = window.matchMedia('(max-width:1479px)');
  let preferredRail;
  try { preferredRail = localStorage.getItem('wb_rail'); } catch { /* Optional visual preference. */ }
  function updateRail() {
    const collapsed = preferredRail ? preferredRail === 'collapsed' : compactRail.matches;
    $('workspace').dataset.rail = collapsed ? 'collapsed' : 'expanded';
    const label = collapsed ? '展开侧栏' : '收起侧栏';
    $('rail-toggle').title = label;
    $('rail-toggle').setAttribute('aria-label', label);
    $('rail-toggle').setAttribute('aria-expanded', String(!collapsed));
    text($('rail-toggle').querySelector('span'), label);
  }
  for (const link of document.querySelectorAll('.app-sidebar .brand, .app-sidebar [data-route], #new-meeting, #import-audio')) {
    link.title = link.textContent.trim(); link.setAttribute('aria-label', link.title);
  }
  updateRail(); compactRail.addEventListener('change', updateRail);
  $('rail-toggle').addEventListener('click', () => {
    preferredRail = $('workspace').dataset.rail === 'collapsed' ? 'expanded' : 'collapsed';
    try { localStorage.setItem('wb_rail', preferredRail); } catch { /* In-memory preference remains active. */ }
    updateRail();
  });
  document.querySelectorAll('button[data-route]').forEach(button => button.addEventListener('click', () => setRoute(button.dataset.route)));
  $('back-library').addEventListener('click', () => setRoute('meetings'));
  $('back-questions').addEventListener('click', () => {
    const from = state.returnNote;
    setMeetingTab(from ? 'transcript' : 'questions');
    if (!from) return;
    transcriptViewport().scrollTop = from.scrollTop;
    const note = [...$('transcript-list').querySelectorAll('details[data-note-id]')].find(node => node.dataset.noteId === from.id);
    (note?.querySelector('summary') || $('transcript-list')).focus({preventScroll: true});
  });
  $('return-meeting').addEventListener('click', (event) => { if (event.currentTarget.dataset.meetingId) selectMeeting(event.currentTarget.dataset.meetingId); });
  for (const id of ['library-list', 'recent-meetings']) $(id).addEventListener('click', (event) => {
    const actions = event.target.closest('[data-manage-meeting]');
    if (actions) { showMeetingActions(actions); return; }
    const row = event.target.closest('[data-meeting-id]');
    if (row?.dataset.materialQuestion && state.route === 'materials' && desktopLibrary.matches) {
      materialShelf.id = row.dataset.rowId; renderLibrary(); return;
    }
    if (row) selectMeeting(row.dataset.meetingId, row.dataset.materialQuestion || '', row.dataset.materialQuestion ? 'document' : 'answer');
  });
  $('library-resume').addEventListener('click', event => { if (event.currentTarget.dataset.meetingId) selectMeeting(event.currentTarget.dataset.meetingId); });
  $('material-filters').addEventListener('click', event => {
    const btn = event.target.closest('[data-material-filter]'); if (!btn) return;
    materialShelf.filter = btn.dataset.materialFilter;
    for (const item of $('material-filters').querySelectorAll('button')) item.setAttribute('aria-pressed', String(btn === item));
    renderLibrary();
  });
  $('material-open').addEventListener('click', () => {
    const r = state.home?.materials?.find(row => row.id === materialShelf.id);
    if (r) selectMeeting(r.meeting_id, r.question_id, 'document');
  });
  $('material-reload').addEventListener('click', () => { const r = state.home?.materials?.find(row => row.id === materialShelf.id); if (r) previewLibraryMaterial(r, true); });
  desktopLibrary.addEventListener('change', () => { if (state.route === 'materials') renderLibrary(); });
  $('meeting-actions-menu').addEventListener('toggle', event => {
    if (event.newState === 'closed') document.querySelectorAll('.meeting-row-actions[aria-expanded="true"]').forEach(button => button.setAttribute('aria-expanded', 'false'));
  });
  $('main').addEventListener('scroll', closeMeetingActions, {passive:true});
  window.addEventListener('resize', closeMeetingActions);
  $('rename-meeting-action').addEventListener('click', openRenameMeeting);
  $('delete-meeting-action').addEventListener('click', openDeleteMeeting);
  $('rename-meeting-form').addEventListener('submit', async event => {
    event.preventDefault(); const meeting = state.manageTarget;
    if (!meeting || $('rename-meeting-submit').disabled) return;
    const title = $('rename-meeting-title').value.trim();
    if (!title) { text($('rename-meeting-error'), '请输入会议名称。'); $('rename-meeting-title').focus(); return; }
    $('rename-meeting-submit').disabled = true; text($('rename-meeting-error'), '');
    try {
      const renamed = await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}`, {method:'PATCH', body:{title}});
      if (state.homeRequest) await state.homeRequest.catch(() => {});
      const row = state.home?.meetings.find(m => m.id === meeting.id);
      if (row) Object.assign(row, renamed);
      if (state.snapshot?.meeting.id === meeting.id) { Object.assign(state.snapshot.meeting, renamed); renderSnapshot(); }
      renderMeetingOptions(); renderLibrary(); $('rename-meeting-dialog').close();
      await refreshHome(); notice('会议名称已更新。');
    } catch (error) { text($('rename-meeting-error'), error.message); }
    finally { $('rename-meeting-submit').disabled = false; }
  });
  $('delete-meeting-form').addEventListener('submit', async event => {
    event.preventDefault(); const meeting = state.manageTarget;
    if (!meeting || !state.deletionReady || $('confirm-delete-meeting').disabled) return;
    $('confirm-delete-meeting').disabled = true; $('cancel-delete-meeting').disabled = true;
    text($('confirm-delete-meeting'), '正在删除…'); text($('delete-meeting-error'), '');
    try {
      if (state.native?.meetingId === meeting.id) await stopCaptureAndWait();
      await api(`/live/v2/meetings/${encodeURIComponent(meeting.id)}`, {method:'DELETE', body:{confirmation:meeting.id}});
      if (state.homeRequest) await state.homeRequest.catch(() => {});
      forgetMeeting(meeting.id); state.manageTarget = null; state.deletionReady = false;
      $('delete-meeting-dialog').close(); await refreshHome();
      notice('会议及其全部本机文件已删除。');
    } catch (error) { text($('delete-meeting-error'), error.message); await refreshHome(); }
    finally {
      $('confirm-delete-meeting').disabled = !state.deletionReady; $('cancel-delete-meeting').disabled = false;
      text($('confirm-delete-meeting'), '删除会议及全部文件');
    }
  });
  $('delete-meeting-dialog').addEventListener('cancel', event => {
    if ($('cancel-delete-meeting').disabled) event.preventDefault();
  });
  $('library-search').addEventListener('input', (event) => {
    state.search = event.target.value;
    if (!['meetings', 'materials'].includes(state.route)) setRoute('meetings');
    renderLibrary();
  });
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(node => node.setAttribute('aria-pressed', String(node === button)));
    renderLibrary();
  }));
  $('library-sort').addEventListener('click', () => { state.descending = !state.descending; renderLibrary(); });
  $('toggle-navigation').addEventListener('click', () => {
    const open = $('workspace').dataset.mobileNav !== 'open';
    $('workspace').dataset.mobileNav = open ? 'open' : 'closed';
    $('toggle-navigation').setAttribute('aria-expanded', String(open));
  });
  $('main').addEventListener('click', () => { $('workspace').dataset.mobileNav = 'closed'; $('toggle-navigation').setAttribute('aria-expanded', 'false'); });
  $('start-listening').addEventListener('click', () => {
    if (state.native?.modelState === 'downloadable') { setRoute('settings'); notice('请先安装中文识别模型，完成后返回当前会议。'); return; }
    nativePost('startCapture', state.meetingId, null, $('capture-source').value);
  });
  $('stop-listening').addEventListener('click', () => nativePost('stopCapture', state.meetingId));
  $('check-model').addEventListener('click', () => nativePost('checkModel'));
  $('install-model').addEventListener('click', () => nativePost('installModel'));
  $('reload-service-settings').addEventListener('click', () => loadServiceSettings(true));
  $('service-settings-form').addEventListener('input', editedServiceSettings);
  $('service-settings-form').addEventListener('change', editedServiceSettings);
  $('service-model-provider').addEventListener('change', () => {
    const deepseek = $('service-model-provider').value === 'deepseek';
    const field = $('service-model-url');
    if (['https://api.deepseek.com', 'https://api.openai.com/v1', ''].includes(field.value.replace(/\/$/, ''))) field.value = deepseek ? 'https://api.deepseek.com' : 'https://api.openai.com/v1';
    if (deepseek && !$('service-model-name').value) $('service-model-name').value = 'deepseek-flash';
    else if (!deepseek && $('service-model-name').value.startsWith('deepseek-')) $('service-model-name').value = '';
    $('service-model-json').checked = deepseek;
    $('service-model-thinking').checked = false;
    $('service-model-effort').value = 'default';
    $('service-model-token-parameter').value = deepseek ? 'max_tokens' : 'max_completion_tokens';
    renderProviderSettings();
  });
  $('service-search-provider').addEventListener('change', () => {
    const tavily = $('service-search-provider').value === 'tavily';
    const field = $('service-search-url');
    if (['https://api.deepseek.com', 'https://api.tavily.com', ''].includes(field.value.replace(/\/$/, ''))) field.value = tavily ? 'https://api.tavily.com' : 'https://api.deepseek.com';
    $('service-search-shared-key').checked = false;
    renderProviderSettings();
  });
  for (const kind of ['model', 'search']) {
    $(`service-${kind}-key`).addEventListener('input', () => {
      if (kind === 'model') state.clearModelKey = false; else state.clearSearchKey = false;
      renderProviderSettings();
    });
    $(`clear-${kind}-key`).addEventListener('click', () => {
      if (kind === 'model') state.clearModelKey = true; else state.clearSearchKey = true;
      $(`service-${kind}-key`).value = '';
      editedServiceSettings();
    });
    $(`test-${kind}-service`).addEventListener('click', () => testService(kind));
  }
  $('service-settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!state.serviceSettings || state.settingsBusy) return;
    const payload = serviceSettingsBody();
    const token = state.token;
    state.settingsBusy = true;
    providerMessage('service-settings-result', '正在保存…');
    renderProviderSettings();
    try {
      const result = await api('/live/v2/settings', {method: 'PUT', body: payload});
      if (state.token !== token) return;
      fillServiceSettings(result);
      state.home.capabilities = result.capabilities;
      renderCapabilities();
      const incomplete = result.capabilities.model_enabled && result.capabilities.search_enabled ? '' : '，尚未配置的服务可稍后补充';
      providerMessage('service-settings-result', `已保存，新任务使用当前配置${incomplete}。`, 'success');
      await refreshHome();
    } catch (error) {
      if (state.token === token) providerMessage('service-settings-result', error.message, 'error');
    } finally { state.settingsBusy = false; renderProviderSettings(); }
  });
  $('settings-open-folder').addEventListener('click', () => nativePost('openOutput'));
  $('theme-preference').addEventListener('change', event => {
    applyTheme(event.target.value);
    try { localStorage.setItem('wb_theme', themePreference); } catch { /* Native preferences are persisted by the host. */ }
    if (nativeAvailable()) nativePost('setThemePreference', null, null, null, themePreference);
  });
  $('open-meeting-folder').addEventListener('click', () => nativePost('openOutput', state.meetingId));
  window.addEventListener('zsjh-native-state', (event) => {
    state.native = event.detail;
    if (themeValues.has(state.native?.themePreference)) applyTheme(state.native.themePreference);
    renderCapture(); renderSettings();
    if ($('meeting-dialog').open) updateMeetingForm();
    if ($('import-dialog').open) updateImportForm();
  });
  window.addEventListener('popstate', () => { if (state.token) readRoute(); });
  window.addEventListener('hashchange', () => { if (state.token) readRoute(); });
  document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => $(button.dataset.closeDialog).close()));
  $('meeting-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const title = $('meeting-title').value.trim();
    if (!title || $('meeting-submit').disabled) return;
    $('meeting-submit').disabled = true;
    text($('meeting-error'), '');
    try {
      const scope = $('meeting-scope').value;
      const audioSource = $('meeting-audio-source').value;
      const research = scope === 'real' && !$('meeting-research').disabled && $('meeting-research').checked;
      const meeting = await api('/live/v2/meetings', {method: 'POST', body: {title, data_scope: scope,
        permissions:{model:research, search:research, auto_search:research}}});
      state.home.meetings = [meeting, ...(state.home.meetings || [])];
      renderMeetingOptions();
      $('meeting-dialog').close();
      await selectMeeting(meeting.id);
      if (scope === 'synthetic') await playDemo(meeting.id);
      else if (nativeAvailable() && state.native?.ready && state.native?.modelState === 'installed') nativePost('startCapture', meeting.id, null, audioSource);
      else notice('会议已创建。请按页面提示开始收听，或手动补充讨论。');
      await refreshHome();
    } catch (error) { text($('meeting-error'), error.message); }
    finally { $('meeting-submit').disabled = false; }
  });
  $('transcript-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const content = $('transcript-input').value.trim();
    const id = state.meetingId;
    if (!content || !id) return;
    await perform($('transcript-submit'), async () => {
      await api(`/live/v2/meetings/${encodeURIComponent(id)}/utterances`, {method: 'POST', body: {event_id: crypto.randomUUID(), segment_id: crypto.randomUUID(), text: content, revision: 1}});
      if (id === state.meetingId) {
        if ($('transcript-input').value.trim() === content) $('transcript-input').value = '';
        await refreshNow();
      }
    });
  });
  $('queue-filters').addEventListener('click', event => {
    const btn = event.target.closest('button[data-queue-filter]');
    if (!btn) return;
    state.queueFilter = btn.dataset.queueFilter;
    renderQuestions(state.snapshot?.questions || []);
    $('question-list').scrollTop = 0;
  });
  $('follow-transcript').addEventListener('change', () => {
    if (!$('follow-transcript').checked) return;
    if (state.transcriptHistory) { state.transcriptHistory = null; renderTranscripts(state.snapshot?.segments || [], true); }
    const viewport = transcriptViewport(); viewport.scrollTop = viewport.scrollHeight;
  });
  $('older-transcript').addEventListener('click', () => earlierTranscripts());
  $('latest-transcript').addEventListener('click', () => {
    state.transcriptHistory = null;
    renderTranscripts(state.snapshot?.segments || [], true);
    $('transcript-list').focus({preventScroll:true});
    const viewport = transcriptViewport(); viewport.scrollTop = viewport.scrollHeight;
  });
  $('transcript-list').addEventListener('click', (event) => {
    const button = event.target.closest(wideMeeting() ? '.note-full, .transcript-note > summary' : '.note-full');
    if (!button) return;
    const id = button.closest('[data-note-id]').dataset.noteId;
    state.returnNote = {id, scrollTop: transcriptViewport().scrollTop};
    openQuestionDetail(id);
  });
  $('locate-question').addEventListener('click', async (event) => {
    const button = event.currentTarget, mid = state.meetingId, id = state.selectedQuestionId;
    const initial = selectedQuestion(), ref = initial?.anchor_ref || initial?.source_refs?.[0];
    if (!ref?.segment_id) return;
    const current = () => {
      const q = selectedQuestion(), anchor = q?.anchor_ref || q?.source_refs?.[0];
      return state.route === 'meeting' && state.meetingId === mid && q?.id === id && q.version === initial.version && !q.basis_stale && q.state !== 'superseded' && anchor?.segment_id === ref.segment_id && anchor.revision === ref.revision && $('work-grid').dataset.mobilePanel === 'results';
    };
    // A note exists only on the exact final segment and revision referenced by this question.
    const find = () => [...$('transcript-list').querySelectorAll('[data-note-id]')].find(n => n.dataset.noteId === id)?.closest('[data-segment-id]');
    button.disabled = true;
    try {
      for (let i = 0; i < 10 && current() && !find() && transcriptPage()?.has_more; i++) {
        const before = transcriptPage().next_before;
        await earlierTranscripts(false);
        if (before === transcriptPage()?.next_before) break;
      }
      if (!current()) return;
      const row = find();
      if (!row) { notice(transcriptPage()?.has_more ? '当前已加载的发言中没有这条依据，可再次定位以继续向前查找。' : '未找到对应修订的原始发言。'); return; }
      if (wideMeeting()) { $('work-grid').dataset.leftPane = 'transcript'; $('meeting-files-panel').hidden = true; }
      else setMeetingTab('transcript');
      $('follow-transcript').checked = false;
      row.tabIndex = -1; row.focus({preventScroll:true}); row.scrollIntoView({block:'nearest'});
    } finally { button.disabled = false; }
  });
  $('question-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-question-id]');
    if (!button) return;
    state.returnNote = null;
    openQuestionDetail(button.dataset.questionId);
  });
  function openQuestionDetail(id) {
    state.selectedQuestionId = id;
    state.detailTab = 'answer';
    state.detailSignature = '';
    document.querySelector('.detail-body').scrollTop = 0;
    renderQuestions(state.snapshot?.questions || []);
    renderDetail();
    setMeetingTab('results');
    $('question-title').tabIndex = -1;
    $('question-title').focus({preventScroll: true});
  }
  document.querySelectorAll('button[data-meeting-tab]').forEach((button) => button.addEventListener('click', () => setMeetingTab(button.dataset.meetingTab)));
  document.querySelectorAll('[data-detail]').forEach((button) => button.addEventListener('click', () => setDetailTab(button.dataset.detail, true)));
  $('new-question').addEventListener('click', () => {
    state.dialogQuestion = null;
    $('question-form').reset();
    text($('question-dialog-heading'), '添加查证问题');
    text($('question-submit'), '开始查证');
    text($('question-dialog-context'), state.snapshot?.meeting?.data_scope === 'real' ? '问题会加入查证队列。未启用联网服务时，页面会保留待处理状态。' : '示例会议使用合成资料演示查证流程。');
    $('new-query-wrap').hidden = state.snapshot?.meeting?.data_scope !== 'real';
    text($('question-dialog-error'), '');
    $('question-dialog').showModal();
    $('question-input').focus();
  });
  $('compose-question').addEventListener('click', () => {
    if ($('new-question').disabled) return;
    const draft = $('transcript-input').value.trim();
    $('new-question').click();
    $('question-input').value = draft.slice(0,$('question-input').maxLength);
    if (draft.length > $('question-input').maxLength) text($('question-dialog-error'), '已带入前 300 字，请确认或缩短问题；完整原文仍保留在讨论输入框。');
  });
  $('edit-question').addEventListener('click', () => {
    const question = selectedQuestion();
    if (!question) return;
    state.dialogQuestion = {id: question.id, version: question.version, meetingId: state.meetingId};
    $('question-input').value = question.title;
    text($('question-dialog-heading'), '修改问题范围');
    text($('question-submit'), '保存新版本');
    $('new-query-wrap').hidden = true;
    text($('question-dialog-context'), `基于版本 ${question.version} 修改，保存后按新范围重新研究。${state.snapshot?.meeting?.data_scope === 'real' ? '新范围需重新确认公开检索词。' : ''}`);
    text($('question-dialog-error'), '');
    $('question-dialog').showModal();
    $('question-input').focus();
  });
  $('question-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const title = $('question-input').value.trim();
    const meetingId = state.dialogQuestion?.meetingId || state.meetingId;
    if (!title || !meetingId || $('question-submit').disabled) return;
    const editing = state.dialogQuestion;
    $('question-submit').disabled = true;
    text($('question-dialog-error'), '');
    try {
      const question = editing
        ? await api(`/live/v2/questions/${encodeURIComponent(editing.id)}`, {method: 'PATCH', body: {expected_version: editing.version, title}})
        : await api(`/live/v2/meetings/${encodeURIComponent(meetingId)}/questions`, {method: 'POST', body: {title, public_query: $('new-query-wrap').hidden ? '' : $('new-public-query').value.trim()}});
      $('question-dialog').close();
      if (meetingId === state.meetingId) {
        state.selectedQuestionId = question.id;
        state.detailTab = 'answer';
        state.detailSignature = '';
        await refreshNow();
        setMeetingTab('results');
      }
    } catch (error) {
      text($('question-dialog-error'), error.status === 409 ? '问题已发生变化。请关闭此窗口，查看最新版本后重新修改。' : error.message);
      if (error.status === 409) await refreshNow();
    } finally { $('question-submit').disabled = false; }
  });
  for (const action of ['approve', 'prioritize', 'retry', 'cancel']) $(`${action}-question`).addEventListener('click', (event) => {
    if (action === 'approve' && state.snapshot?.meeting?.data_scope === 'real' && !selectedQuestion()?.public_query) openQueryDialog();
    else questionAction(action, event.currentTarget);
  });
  $('set-public-query').addEventListener('click', openQueryDialog);
  $('search-query-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const editing = state.queryQuestion;
    const query = $('public-query-input').value.trim();
    if (!editing || !query || $('search-query-submit').disabled) return;
    $('search-query-submit').disabled = true;
    text($('search-query-error'), '');
    try {
      await api(`/live/v2/questions/${encodeURIComponent(editing.id)}/actions`, {method: 'POST', body: {action: 'approve', expected_version: editing.version, public_query: query}});
      $('search-query-dialog').close();
      if (editing.meetingId === state.meetingId) await refreshNow();
    } catch (error) {
      text($('search-query-error'), error.status === 409 ? `问题状态已变化，请核对后重试。${error.message}` : error.message);
      if (error.status === 409) await refreshNow();
    } finally { $('search-query-submit').disabled = false; }
  });
  $('research-control').addEventListener('click', (event) => meetingControl(state.snapshot?.meeting?.research_paused ? 'resume_research' : 'pause_research', event.currentTarget));
  $('retry-coordinator').addEventListener('click', (event) => meetingControl('retry_coordinator', event.currentTarget));
  $('show-meeting-info').addEventListener('click', () => $('meeting-info-dialog').showModal());
  $('show-activity').addEventListener('click', () => $('activity-dialog').showModal());
  $('end-meeting').addEventListener('click', () => {
    state.endTarget = state.meetingId;
    const unfinished = (state.snapshot?.questions || []).filter(q => activeStates.has(q.state)).length;
    const pending = Math.max(0, state.snapshot?.automation_progress?.pending || 0);
    text($('end-description'), `停止收听，保存原始音频和全量转录，并开始整理会议纪要。${pending ? `还有 ${pending} 段转写未完成问题识别。` : ''}${unfinished ? `还有 ${unfinished} 个未完成的查证问题。` : ''}问题识别与未完成查证会暂停；会后点击“恢复自动查证”继续，已有问题也可逐条重试。`);
    text($('end-error'), '');
    $('end-dialog').showModal();
  });
  $('end-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = state.endTarget;
    if (!id || $('confirm-end').disabled) return;
    $('confirm-end').disabled = true;
    text($('end-error'), '');
    try {
      if (state.native?.meetingId === id) await stopCaptureAndWait();
      const archived = await api(`/live/v2/meetings/${encodeURIComponent(id)}/controls`, {method:'POST', body:{action:'end'}});
      $('end-dialog').close();
      if (state.meetingId === id) await refreshNow();
      await refreshHome();
      setMeetingTab('files');
      const failed = Object.values(archived.base_files || {}).some(f => f.state === 'save_failed');
      notice(failed ? '会议已结束，部分文件未能保存。请在会议文件中重新保存。' : '会议已结束。三份基础文件的状态可在这里查看。', failed);
    } catch (error) { text($('end-error'), error.message); }
    finally { $('confirm-end').disabled = false; }
  });
  $('switch-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const target = state.switchTarget;
    if (!target || $('confirm-switch').disabled) return;
    $('confirm-switch').disabled = true;
    try {
      await stopCaptureAndWait();
      state.switchTarget = null;
      $('switch-dialog').close();
      await selectMeeting(target.id, target.questionId, target.tab);
      notice('已暂停上一场会议的收听。');
    } catch (error) { text($('switch-error'), error.message); }
    finally { $('confirm-switch').disabled = false; }
  });
  $('switch-dialog').addEventListener('close', () => {
    if (state.switchTarget) {
      state.switchTarget = null;
      const hash = state.route === 'meeting' ? meetingHash() : `#${state.route}`;
      history.replaceState(null, '', hash);
    }
  });
  document.addEventListener('click', (event) => {
    for (const menu of document.querySelectorAll('.session-menu, .meeting-more')) {
      if (!menu.contains(event.target) || menu.matches('.meeting-more') && event.target.closest('button')) menu.open = false;
    }
  });
  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && !document.querySelector('dialog[open]')) { event.preventDefault(); $('library-search').focus(); $('library-search').select(); }
    if (event.key === 'Escape') {
      for (const menu of document.querySelectorAll('.meeting-more[open], .session-menu[open]')) { menu.open = false; menu.querySelector('summary')?.focus(); }
      $('workspace').dataset.mobileNav = 'closed';
      $('toggle-navigation').setAttribute('aria-expanded', 'false');
    }
  });
  $('run-demo').addEventListener('click', (event) => {
    const id = state.meetingId;
    perform(event.currentTarget, async () => {
      await playDemo(id);
      notice('示例流程已开始。打开“查证问题”查看生成的资料。');
    });
  });
  $('download-document').addEventListener('click', (event) => {
    const question = selectedQuestion();
    if (!question || !artifactID(question)) return;
    perform(event.currentTarget, async () => {
      const artifact = await getArtifact(question, true);
      const blob = new Blob([artifact.content || ''], {type: 'text/markdown;charset=utf-8'});
      const objectURL = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = objectURL;
      const basename = (artifact.path || '').split(/[\\/]/).pop();
      anchor.download = (basename || `${question.title || '研究回答'}.md`).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 180);
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(objectURL), 1000);
    });
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); refreshHome(); if (nativeAvailable()) nativePost('requestState'); } });
  new ResizeObserver(() => {
    $('meeting-view').style.setProperty('--meeting-toolbar-height', `${document.querySelector('.meeting-toolbar').offsetHeight + 12}px`);
  }).observe(document.querySelector('.meeting-toolbar'));
  setInterval(() => { if (!document.hidden) { renderResearchClock(); refresh(); } }, 1000);
  setInterval(() => { if (!document.hidden) refreshHome(); }, 5000);
  let saved = '';
  try { saved = sessionStorage.getItem('wb_access') || ''; } catch { /* Session storage is optional. */ }
  if (saved) login(saved).catch((error) => { logout(false); text($('login-error'), error.message); });
})();
