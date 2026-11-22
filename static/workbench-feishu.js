/* Settings for delivery of the workbench's saved research documents. */
'use strict';
window.createFeishuSettings = function ({api, state, $, element, text, safeURL}) {
  const section = $('feishu-section');
  const get = name => document.getElementById('feishu-' + name);
  const call = (path, body, method = 'POST') => api('/live/v2/feishu/' + path, body === undefined ? {} : {method, body});
  const active = () => !!state.token && state.home?.member?.role === 'owner';
  const visible = () => active() && state.route === 'settings' && !$('prefs-pane-feishu').hidden && !document.hidden;
  let settings, status, working, session = 0, dirty = false, jobsSignature = '', authURL = '', pendingTest = '';
  let refreshingJobs = false;
  const feedback = (message = '', error = false) => {
    text(get('feedback'), message); get('feedback').dataset.state = error ? 'error' : '';
  };
  function destinationURL(value) {
    const url = safeURL(value);
    return url?.protocol === 'https:' && /(^|\.)(feishu\.cn|larksuite\.com|larkoffice\.com)$/.test(url.hostname) ? url : null;
  }
  function renderControls() {
    const busy = !!working;
    const wantsChat = get('delivery').value === 'chat';
    const enabled = get('enabled').checked;
    const locked = busy || !settings || !enabled;
    get('enabled').disabled = busy || !settings;
    for (const name of ['folder', 'delivery', 'chat-query', 'chat-choice']) get(name).disabled = locked;
    get('chat-controls').hidden = !wantsChat;
    text(get('connect'), status?.authorized ? '重新授权' : '连接飞书');
    get('connect').disabled = busy || !status?.configured;
    get('refresh').disabled = busy;
    get('auth-link').hidden = !authURL;
    get('auth-done').hidden = !authURL;
    get('auth-done').disabled = busy;
    get('check-folder').disabled = locked || !status?.authorized || !folderInput();
    get('chat-search').disabled = locked || !status?.authorized || !get('chat-query').value.trim();
    const valid = !enabled || (status?.authorized && folderInput() && (!wantsChat || get('chat-choice').value));
    get('save').disabled = busy || !settings || !dirty || !valid;
    get('test').disabled = busy || !settings?.enabled || !settings?.folder_token || dirty || !status?.authorized || !!pendingTest;
    let hint = '';
    if (enabled) {
      if (!status?.authorized) hint = '请先连接飞书账号。';
      else if (!folderInput()) hint = '请粘贴飞书云空间文件夹链接。群通知会附上保存在这里的文档。';
      else if (wantsChat && !get('chat-choice').value) hint = '请搜索并选中接收通知的群聊。';
      else if (!dirty && settings?.enabled) hint = `测试将在「${settings.folder_name}」创建合成文档${settings.chat_id ? `，并通知「${settings.chat_name}」` : ''}。`;
    }
    if (!hint && dirty) hint = '请点击「保存飞书设置」让修改生效。';
    text(get('test-info'), hint);
    const savedState = settings?.enabled ? '已开启' : '未开启';
    text(get('badge'), settings ? (dirty ? `${savedState} · 修改未保存` : savedState) : '正在读取');
    for (const button of get('jobs').querySelectorAll('button')) button.disabled = busy || !settings?.enabled;
  }
  function setStatus(value) {
    status = value;
    text(get('account'), value.authorized ? `已连接${value.user_name ? ' · ' + value.user_name : ''}` : value.message || '请连接飞书账号。');
    if (value.authorized) clearAuthorization();
  }
  function clearAuthorization() {
    authURL = ''; get('auth-link').removeAttribute('href');
  }
  function folderInput() { return get('folder').value.trim() || settings?.folder_token || ''; }
  function fill(value) {
    settings = value; dirty = false;
    get('enabled').checked = !!value.enabled;
    get('folder').value = '';
    get('folder').placeholder = value.folder_token ? '粘贴新链接以更换文件夹' : '粘贴飞书云空间文件夹链接';
    text(get('folder-name'), value.folder_name ? '已保存的文件夹：' + value.folder_name : '');
    get('delivery').value = value.chat_id ? 'chat' : 'folder';
    get('chat-query').value = '';
    get('chat-choice').replaceChildren(new Option('请选择群聊', ''));
    if (value.chat_id) get('chat-choice').add(new Option(value.chat_name || value.chat_id, value.chat_id, false, true));
  }
  function renderJobs(jobs) {
    if (pendingTest && !jobs.some(job => job.id === pendingTest && ['queued','running'].includes(job.state))) pendingTest = '';
    const signature = JSON.stringify(jobs);
    if (signature === jobsSignature) return;
    jobsSignature = signature;
    const list = get('jobs'); list.replaceChildren();
    if (!jobs.length) list.append(element('li', 'feishu-caption', '暂无同步记录。'));
    for (const job of jobs) {
      const item = element('li', 'feishu-job');
      const head = element('div', 'feishu-job-head');
      head.append(element('strong', '', job.title || '查证结果'), element('span', '', job.state_label));
      item.append(head);
      if (job.error) item.append(element('p', 'feishu-job-error', job.error));
      const url = destinationURL(job.url);
      if (url) {
        const link = element('a', '', '打开飞书文档 ↗'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; item.append(link);
      }
      if (job.retryable) {
        const uncertain = job.state === 'unknown';
        const button = element('button', 'text-button', uncertain ? '已核对，重新发送' : job.stage === 'message' ? '重试通知' : '重试同步');
        button.type = 'button'; button.dataset.retry = job.id;
        if (uncertain) {
          const confirmation = element('details');
          confirmation.append(element('summary', '', '核对后重试'), element('p', '', `请先在飞书${job.stage === 'message' ? '群聊' : '文件夹'}中确认没有收到本次结果，重新发送可能产生重复。`), button);
          button.dataset.confirmUnknown = 'true'; item.append(confirmation);
        } else item.append(button);
      }
      list.append(item);
    }
  }
  // An old account's slow response must not change the next session's form.
  async function act(task, label = '', failed = null) {
    if (!active() || working) return;
    const token = state.token, generation = session;
    const current = () => active() && token === state.token && generation === session;
    working = {}; renderControls(); if (label) feedback(label);
    try { await task(current); }
    catch (error) { if (current()) feedback(failed ? failed(error) : error.message, true); }
    finally { if (current()) { working = null; renderControls(); } }
  }
  async function load() {
    await act(async current => {
      const [config, account] = await Promise.allSettled([call('settings'), call('status')]);
      if (!current()) return;
      if (config.status === 'fulfilled') {
        if (!dirty) fill(config.value.settings);
        renderJobs(config.value.jobs || []);
      }
      if (account.status === 'fulfilled') setStatus(account.value);
      else setStatus({message: '账号状态检查失败。' + account.reason.message});
      if (config.status === 'rejected') throw config.reason;
      feedback(dirty ? '已刷新账号状态，修改尚未保存。' : '');
    }, '正在读取飞书设置…');
  }
  function reset() {
    session++; settings = status = working = null; dirty = false; pendingTest = ''; refreshingJobs = false;
    clearAuthorization(); jobsSignature = ''; get('jobs').replaceChildren(); get('form').reset(); get('chat-choice').replaceChildren();
    text(get('account'), '正在检查账号…'); text(get('folder-name'), ''); feedback(); section.hidden = true;
  }
  function open() {
    section.hidden = !active(); if (!section.hidden) load();
  }
  get('refresh').addEventListener('click', load);
  get('connect').addEventListener('click', () => act(async current => {
    const result = await call('auth/start', {});
    if (!current()) return;
    const url = destinationURL(result.verification_url);
    if (!url) throw new Error('未取得有效的飞书授权地址，请刷新后重试。');
    authURL = url.href; get('auth-link').href = authURL;
    feedback('打开飞书授权页，确认后回到这里点击「我已完成授权」。');
  }, '正在准备授权…'));
  get('auth-done').addEventListener('click', () => act(async current => {
    const result = await call('auth/poll', {});
    if (!current()) return;
    if (!result.authorized) { feedback(result.message || '等待飞书授权。'); return; }
    const account = await call('status');
    if (current()) { setStatus(account); feedback(account.authorized ? '飞书账号已连接。' : account.message); }
  }, '正在确认授权…'));
  get('check-folder').addEventListener('click', () => act(async current => {
    const folder = await call('folder', {folder:folderInput()});
    if (current()) { text(get('folder-name'), folder.name); feedback('已找到文件夹。'); }
  }, '正在检查文件夹…'));
  get('chat-search').addEventListener('click', () => act(async current => {
    const result = await call('chats', {query:get('chat-query').value.trim()});
    if (!current()) return;
    const select = get('chat-choice'), previous = select.selectedOptions[0];
    select.replaceChildren(new Option('请选择群聊', ''));
    if (previous?.value) select.add(new Option(previous.text, previous.value, false, true));
    for (const chat of result.items || []) if (chat.id && chat.id !== previous?.value) select.add(new Option(chat.name || chat.id, chat.id));
    feedback(result.items?.length ? '从结果中选择接收通知的群。' : '未找到匹配的群，请检查群名和账号。');
  }, '正在搜索群聊…'));
  get('form').addEventListener('input', event => {
    if (activeHelp) help(activeHelp, false);
    if (event.target === get('folder')) text(get('folder-name'), '');
    if (event.target !== get('chat-query')) dirty = true;
    feedback(); renderControls();
  });
  get('chat-query').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); get('chat-search').click(); } });
  get('form').addEventListener('submit', event => {
    event.preventDefault(); if (get('save').disabled) return;
    act(async current => {
      const chat = get('delivery').value === 'chat' ? get('chat-choice').selectedOptions[0] : null;
      const value = await call('settings', {enabled:get('enabled').checked, folder:folderInput(), chat_id:chat?.value || '', chat_name:chat?.value ? chat.text : '', revision:settings.revision}, 'PUT');
      if (current()) { fill(value.settings); renderJobs(value.jobs || []); feedback(value.settings.enabled ? '已保存。新保存的查证结果将自动同步。' : '已保存，自动同步已关闭。'); }
    }, '正在保存飞书设置…', error => {
      if (error.status === 409 && error.message.includes('设置已更新')) { dirty = false; settings = null; return '设置已在其他窗口更改，请点击「刷新状态」重新载入。'; }
      return error.message;
    });
  });
  get('test').addEventListener('click', () => act(async current => {
    const result = await call('test', {});
    if (current()) { pendingTest = result.id; get('recent').open = true; feedback(result.message || '测试已提交，请在最近同步中查看结果。'); await refreshJobs(current); }
  }, '正在发送合成测试…', error => {
    if (!error.status || error.status >= 500) { get('recent').open = true; return `测试结果待核对。请在飞书和最近同步中查看，避免重复发送。${error.message}`; }
    return error.message;
  }));
  get('jobs').addEventListener('click', event => {
    const button = event.target.closest('button'), id = button?.dataset.retry;
    if (id) act(async current => {
      await call(`jobs/${encodeURIComponent(id)}/retry`, {confirm_unknown:button.dataset.confirmUnknown === 'true'});
      if (current()) { feedback('已重新排队。'); await refreshJobs(current); }
    }, '正在重新排队…');
  });
  async function refreshJobs(current) {
    const value = await call('settings');
    if (current()) { renderJobs(value.jobs || []); renderControls(); }
  }
  setInterval(async () => {
    if (!visible() || (!pendingTest && !get('recent').open) || refreshingJobs || working) return;
    const generation = session; refreshingJobs = true;
    try { await refreshJobs(() => generation === session && visible() && !working); }
    catch (error) { if (generation === session && visible()) feedback('同步记录读取失败。' + error.message, true); }
    finally { if (generation === session) refreshingJobs = false; }
  }, 5000);
  let activeHelp, pinned = false;
  function help(button, show, pin = false) {
    if (activeHelp && activeHelp !== button) help(activeHelp, false);
    const tip = $(button.getAttribute('aria-describedby'));
    tip.hidden = !show; button.setAttribute('aria-expanded', String(show)); activeHelp = show ? button : null; pinned = show && pin;
  }
  for (const wrap of section.querySelectorAll('.feishu-help')) {
    const button = wrap.querySelector('button');
    wrap.addEventListener('pointerenter', e => { if (e.pointerType !== 'touch' && activeHelp !== button) help(button, true); });
    wrap.addEventListener('pointerleave', () => { if (activeHelp === button && !pinned && document.activeElement !== button) help(button, false); });
    button.addEventListener('focus', () => { if (activeHelp !== button) help(button, true); });
    button.addEventListener('blur', () => { if (activeHelp === button) help(button, false); });
    button.addEventListener('click', () => { const show = activeHelp !== button || !pinned; help(button, show, show); });
  }
  const closeHelpOutside = event => { if (activeHelp && !activeHelp.parentElement.contains(event.target)) help(activeHelp, false); };
  document.addEventListener('pointerdown', closeHelpOutside);
  section.addEventListener('focusin', closeHelpOutside);
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && activeHelp) { help(activeHelp, false); event.preventDefault(); } });
  return {open, reset};
};
