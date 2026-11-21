/* 只通过宿主的 MCP Apps 桥接访问本机数据。 */
const byId = (id) => document.getElementById(id);
const pendingRequests = new Map();
const protocolVersion = '2026-01-26';
let nextRequest = 1;
let hostOrigin = '*';

function post(message) {
  window.parent.postMessage({ jsonrpc: '2.0', ...message }, hostOrigin);
}

function request(method, params, timeout = 15000) {
  const id = nextRequest++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingRequests.delete(id);
      reject(new Error('本机插件响应超时，请重新打开面板。'));
    }, timeout);
    pendingRequests.set(id, { resolve, reject, timer });
    post({ id, method, params });
  });
}

function applyTheme(context) {
  if (['dark', 'light'].includes(context?.theme)) {
    document.documentElement.dataset.theme = context.theme;
  }
}

window.addEventListener('message', (event) => {
  if (event.source !== window.parent || (hostOrigin !== '*' && event.origin !== hostOrigin)) return;
  const message = event.data;
  if (message?.jsonrpc !== '2.0') return;
  if (message.method === 'ui/notifications/host-context-changed') applyTheme(message.params);
  if (message.method === 'ui/resource-teardown' && message.id !== undefined) {
    post({ id: message.id, result: {} });
  }
  const pending = pendingRequests.get(message.id);
  if (!pending) return;
  clearTimeout(pending.timer);
  pendingRequests.delete(message.id);
  if (hostOrigin === '*' && event.origin && event.origin !== 'null') hostOrigin = event.origin;
  if (message.error) pending.reject(new Error(message.error.message || '插件调用失败。'));
  else pending.resolve(message.result);
});

async function call(name, args = {}, timeout) {
  const result = await request('tools/call', { name, arguments: args }, timeout);
  if (result?.isError) throw new Error(result.content?.[0]?.text || '操作失败。');
  if (!result?.structuredContent) throw new Error('本机插件没有返回数据。');
  return result.structuredContent;
}

const personaText = byId('persona-text');
const memoryTitle = byId('memory-title');
const memoryTags = byId('memory-tags');
const memoryBody = byId('memory-body');
const memoryRows = byId('memory-rows');
const memorySearch = byId('memory-search');
const autoMemory = byId('auto-memory');
const confirmDialog = byId('confirm-dialog');
const searchState = { sequence: 0, timer: null, pending: 0, rows: [], query: null };
let persona = null;
let selected = null;
let connected = false;
let writing = false;

function status(message = '', error = false) {
  const node = byId('status');
  node.textContent = message;
  node.dataset.error = String(error);
}

async function perform(action) {
  try {
    return await action();
  } catch (error) {
    status(error.message || '操作失败。', true);
  }
}

function confirmAction(title, description, label) {
  byId('confirm-title').textContent = title;
  byId('confirm-description').textContent = description;
  byId('confirm-action').textContent = label;
  confirmDialog.returnValue = 'cancel';
  confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmDialog.addEventListener('close', () => resolve(confirmDialog.returnValue === 'accept'), { once: true });
  });
}

for (const [button, value] of [['cancel-action', 'cancel'], ['confirm-action', 'accept']]) {
  byId(button).addEventListener('click', () => confirmDialog.close(value));
}

function personaChanged() {
  return persona !== null && personaText.value !== persona.text.replace(/\r\n?/g, '\n');
}

function memoryChanged() {
  return memoryTitle.value !== (selected?.title ?? '') || memoryTags.value !== (selected?.tags ?? '')
    || memoryBody.value !== (selected?.body ?? '').replace(/\r\n?/g, '\n');
}

function showPersona(value) {
  persona = value;
  personaText.value = value.text;
  personaText.disabled = false;
  byId('persona-file').textContent = value.filename;
  updateControls();
}

function updateControls() {
  byId('reload-persona').disabled = !connected || writing;
  byId('save-persona').disabled = writing || !personaChanged() || !personaText.value.trim();
  byId('new-memory').disabled = !connected || writing;
  byId('save-memory').disabled = !connected || writing || !memoryChanged()
    || !memoryTitle.value.trim() || !memoryBody.value.trim();
  byId('delete-memory').disabled = writing;
  byId('delete-memory').hidden = selected === null;
}

async function write(action) {
  if (writing) return;
  writing = true;
  // 写入期间固定表单，避免请求完成时覆盖刚输入的内容。
  for (const input of [personaText, memoryTitle, memoryTags, memoryBody]) input.readOnly = true;
  updateControls();
  try {
    await perform(action);
  } finally {
    writing = false;
    for (const input of [personaText, memoryTitle, memoryTags, memoryBody]) input.readOnly = false;
    updateControls();
  }
}

byId('reload-persona').addEventListener('click', () => perform(async () => {
  if (personaChanged() && !await confirmAction('放弃本次修改？', '重新加载会读取磁盘上的人设卡。', '重新加载')) return;
  showPersona(await call('read_persona'));
  status('已重新加载人设卡。');
}));
byId('save-persona').addEventListener('click', () => {
  if (persona === null || !personaChanged()) return;
  write(async () => {
    showPersona(await call('save_persona', { version: persona.version, text: personaText.value }));
    status('人设已保存。新聊天将读取更新后的内容。');
  });
});

// 标签必须独立匹配，避免把名称相似的自定义标签误认为固定标签。
function isFixedMemory(item) {
  return item?.tags.split(/[,，、]/).some((tag) => tag.trim() === '用户固定') ?? false;
}

function showMemory(item) {
  selected = item;
  memoryTitle.value = item?.title ?? '';
  memoryTags.value = item?.tags ?? '';
  memoryBody.value = item?.body ?? '';
  byId('editor-title').textContent = item ? '编辑记忆' : '新建记忆';
  byId('editor-detail').textContent = item
    ? `${item.origin === 'automatic' ? '自动记录' : '手动保存'} · 更新于 ${new Date(item.updated).toLocaleString('zh-CN')}${isFixedMemory(item) ? ' · 用户固定' : ''}`
    : '填写跨项目通用的偏好或长期约定。';
  updateControls();
  renderRows();
}

async function selectMemory(item) {
  if (writing || (item && selected?.id === item.id && selected.revision === item.revision)) return;
  if (memoryChanged() && !await confirmAction('放弃未保存的记忆？', '当前表单中的改动将被丢弃。', '放弃修改')) return;
  showMemory(item);
  if (item === null) memoryTitle.focus();
  status();
}

function renderRows() {
  memoryRows.replaceChildren();
  if (searchState.rows.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = memorySearch.value.trim() ? '没有匹配的记忆。' : '还没有记忆。';
    memoryRows.append(empty);
    return;
  }
  for (const item of searchState.rows) {
    const row = document.createElement('button');
    row.className = 'row';
    row.type = 'button';
    row.setAttribute('aria-current', String(selected?.id === item.id));
    const heading = document.createElement('strong');
    heading.textContent = item.title;
    const summary = document.createElement('span');
    summary.textContent = item.tags || item.body.slice(0, 90);
    row.append(heading, summary);
    row.addEventListener('click', () => perform(() => selectMemory(item)));
    memoryRows.append(row);
  }
}

// 后台刷新与手动查询共用序号，确保过期结果不会覆盖新的筛选条件。
async function refreshMemory({ background = false } = {}) {
  clearTimeout(searchState.timer);
  searchState.timer = null;
  const sequence = ++searchState.sequence;
  const query = memorySearch.value;
  searchState.pending += 1;
  try {
    const { items } = await call('list_memories', { query });
    if (sequence !== searchState.sequence || (background && (writing || confirmDialog.open))) return;
    const fresh = items.find((item) => item.id === selected?.id);
    const updateEditor = selected && !memoryChanged()
      && (fresh ? fresh.revision !== selected.revision : !query.trim());
    // 列表未改变时保留节点，避免定期刷新打断键盘焦点。
    if (!updateEditor && searchState.query === query && searchState.rows.length === items.length
      && searchState.rows.every((item, index) => item.id === items[index].id && item.revision === items[index].revision)) return;
    searchState.query = query;
    searchState.rows = items;
    if (updateEditor) showMemory(fresh ?? null);
    else renderRows();
  } finally {
    searchState.pending -= 1;
  }
}

memorySearch.addEventListener('input', () => {
  ++searchState.sequence;
  clearTimeout(searchState.timer);
  searchState.timer = setTimeout(() => perform(refreshMemory), 220);
});
byId('new-memory').addEventListener('click', () => perform(() => selectMemory(null)));
byId('save-memory').addEventListener('click', () => perform(async () => {
  if (!connected || writing || !memoryChanged()) return;
  const current = selected;
  const fields = { title: memoryTitle.value, tags: memoryTags.value, body: memoryBody.value };
  const fixed = isFixedMemory(current);
  if (fixed && !await confirmAction('修改「用户固定」记忆？', current.title, '确认修改')) return;
  await write(async () => {
    const item = current
      ? await call('edit_memory', { ...fields, id: current.id, revision: current.revision, confirm_fixed: fixed })
      : await call('add_memory', fields);
    showMemory(item);
    await refreshMemory();
    status('记忆已保存。');
  });
}));
byId('delete-memory').addEventListener('click', () => perform(async () => {
  if (!selected || writing) return;
  const { id, revision, title } = selected;
  const fixed = isFixedMemory(selected);
  const question = fixed ? '删除「用户固定」记忆？' : '删除这条记忆？';
  if (!await confirmAction(question, title, '删除记忆')) return;
  await write(async () => {
    await call('delete_memory', { id, revision, confirm_fixed: fixed });
    showMemory(null);
    await refreshMemory();
    status('记忆已删除。');
  });
}));
for (const input of [personaText, memoryTitle, memoryTags, memoryBody]) {
  input.addEventListener('input', updateControls);
}

autoMemory.addEventListener('change', () => perform(async () => {
  const next = autoMemory.checked;
  autoMemory.disabled = true;
  try {
    const { enabled } = await call('set_auto_memory', { enabled: next });
    autoMemory.checked = enabled;
    status(enabled ? '自动记忆已开启。' : '自动记忆已关闭。');
  } catch (error) {
    autoMemory.checked = !next;
    throw error;
  } finally {
    autoMemory.disabled = false;
  }
}));

const tabs = [byId('persona-tab'), byId('memory-tab'), byId('history-tab')];
const memoryPanel = byId('memory');
const pluginVersion = document.querySelector('main').dataset.version;
const updateButton = byId('update-button');
const updateDialog = byId('update-dialog');
let availableUpdate = null;
let installingUpdate = false;
let updateCheckBusy = false;
let updatePoll = null;

// 发布说明按纯文本插入，GitHub 上的内容不能生成面板元素。
function displayReleaseNotes(container, releases) {
  container.replaceChildren();
  for (const release of releases) {
    const heading = document.createElement('h3');
    heading.textContent = `版本 ${release.version}`;
    const notes = document.createElement('p');
    notes.textContent = release.notes;
    container.append(heading, notes);
  }
}

async function loadUpdateHistory() {
  const { items } = await call('update_history');
  const list = byId('history-rows');
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '目前没有正式发布的更新记录。';
    list.append(empty);
  }
  for (const item of items) {
    const entry = document.createElement('details');
    entry.className = 'history-entry';
    const summary = document.createElement('summary');
    const published = item.published_at ? new Date(item.published_at).toLocaleDateString('zh-CN') : '';
    summary.textContent = `版本 ${item.version}${published ? ` · ${published}` : ''}`;
    const notes = document.createElement('div');
    notes.className = 'release-notes';
    const body = document.createElement('p');
    body.textContent = item.notes;
    notes.append(body);
    if (item.installed_at) {
      const installed = document.createElement('p');
      installed.textContent = `本机安装于 ${new Date(item.installed_at).toLocaleString('zh-CN')}`;
      notes.append(installed);
    }
    entry.append(summary, notes);
    list.append(entry);
  }
}

// 首次打开、回到页面和手动重试共用检查状态，检查期间保持面板可用。
async function checkForUpdate(refresh = true) {
  if (!connected || installingUpdate || updateCheckBusy || updateDialog.open) return;
  updateCheckBusy = true;
  clearTimeout(updatePoll);
  updateButton.disabled = true;
  updateButton.textContent = '检查更新中…';
  try {
    const state = await call('check_updates', { refresh });
    byId('installed-version').textContent = `版本 ${state.current}`;
    if (state.checking) {
      updatePoll = setTimeout(() => checkForUpdate(false), 450);
      return;
    }
    availableUpdate = state.update ? { ...state.update, current: state.current } : null;
    updateButton.disabled = !availableUpdate && !state.error;
    updateButton.classList.toggle('primary', Boolean(availableUpdate));
    updateButton.textContent = availableUpdate ? '可更新' : state.error ? '重试检查' : '已是最新';
    byId('update-status').textContent = state.error
      || (state.current !== state.running ? '请重新打开面板或开启新聊天以载入已安装版本。' : '');
    if (!byId('history').hidden) perform(loadUpdateHistory);
  } catch (error) {
    availableUpdate = null;
    updateButton.disabled = false;
    updateButton.classList.remove('primary');
    updateButton.textContent = '重试检查';
    byId('update-status').textContent = error.message;
  } finally {
    updateCheckBusy = false;
  }
}

updateButton.addEventListener('click', () => {
  if (!availableUpdate) return checkForUpdate();
  byId('update-title').textContent = `更新到 ${availableUpdate.version}`;
  byId('update-versions').textContent = `当前版本 ${availableUpdate.current} → ${availableUpdate.version}`;
  displayReleaseNotes(byId('update-notes'), availableUpdate.releases);
  byId('install-status').textContent = '';
  byId('install-status').dataset.error = 'false';
  updateDialog.showModal();
});
byId('cancel-update').addEventListener('click', () => updateDialog.close());
updateDialog.addEventListener('cancel', (event) => {
  if (installingUpdate) event.preventDefault();
});
byId('install-update').addEventListener('click', async () => {
  if (!availableUpdate || installingUpdate) return;
  installingUpdate = true;
  const accepted = availableUpdate;
  const installStatus = byId('install-status');
  byId('cancel-update').disabled = true;
  byId('install-update').disabled = true;
  installStatus.dataset.error = 'false';
  installStatus.textContent = '正在下载并安装更新…';
  try {
    const result = await call('install_update', { commit: accepted.commit }, 75000);
    availableUpdate = null;
    updateDialog.close();
    byId('installed-version').textContent = `版本 ${result.current}`;
    byId('update-status').textContent = '更新已安装。请重新打开面板或开启新聊天。';
    updateButton.textContent = '已是最新';
    updateButton.disabled = true;
    updateButton.classList.remove('primary');
    await perform(loadUpdateHistory);
  } catch (error) {
    installStatus.dataset.error = 'true';
    installStatus.textContent = error.message;
  } finally {
    installingUpdate = false;
    byId('cancel-update').disabled = false;
    byId('install-update').disabled = false;
  }
});
window.addEventListener('focus', () => checkForUpdate());
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) checkForUpdate();
});

setInterval(() => {
  if (!connected || document.hidden || memoryPanel.hidden || writing || confirmDialog.open
    || searchState.timer !== null || searchState.pending) return;
  // 面板与聊天同时打开时同步聊天内的更新，失败后在下一次轮询重试。
  refreshMemory({ background: true }).catch(() => {});
}, 5000);

function selectTab(tab) {
  for (const item of tabs) {
    const active = item === tab;
    item.setAttribute('aria-selected', String(active));
    item.tabIndex = active ? 0 : -1;
    byId(item.getAttribute('aria-controls')).hidden = !active;
  }
  status();
  if (connected && tab === tabs[1]) perform(refreshMemory);
  if (connected && tab === tabs[2]) {
    perform(loadUpdateHistory);
    checkForUpdate();
  }
}
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const offset = event.key === 'ArrowLeft' ? -1 : 1;
    const nextIndex = (index + offset + tabs.length) % tabs.length;
    const target = tabs[event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : nextIndex];
    selectTab(target);
    target.focus();
  });
}

updateControls();
perform(async () => {
  const host = await request('ui/initialize', {
    appInfo: { name: 'dragon-memory', version: pluginVersion },
    appCapabilities: { availableDisplayModes: ['inline', 'fullscreen'] },
    protocolVersion,
  });
  applyTheme(host?.hostContext);
  post({ method: 'ui/notifications/initialized' });
  connected = true;
  updateControls();
  byId('installed-version').textContent = `版本 ${pluginVersion}`;
  checkForUpdate();
  const [card, settings] = await Promise.all([call('read_persona'), call('get_auto_memory')]);
  showPersona(card);
  autoMemory.checked = settings.enabled;
  autoMemory.disabled = false;
  status();
  if (!memoryPanel.hidden && searchState.query === null && !searchState.pending) await refreshMemory();
});
