/* 只通过宿主的 MCP Apps 桥接访问本机数据。 */
const byId = (id) => document.getElementById(id);
const pendingRequests = new Map();
const protocolVersion = '2026-01-26';
let nextRequest = 1;
let hostOrigin = '*';

function post(message) {
  window.parent.postMessage({ jsonrpc: '2.0', ...message }, hostOrigin);
}

function request(method, params) {
  const id = nextRequest++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingRequests.delete(id);
      reject(new Error('本机插件响应超时，请重新打开面板。'));
    }, 15000);
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

async function call(name, args = {}) {
  const result = await request('tools/call', { name, arguments: args });
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
const searchState = { sequence: 0, timer: null, rows: [], loaded: false };
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
  const dialog = byId('confirm-dialog');
  byId('confirm-title').textContent = title;
  byId('confirm-description').textContent = description;
  byId('confirm-action').textContent = label;
  dialog.returnValue = 'cancel';
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'accept'), { once: true });
  });
}

for (const [button, value] of [['cancel-action', 'cancel'], ['confirm-action', 'accept']]) {
  byId(button).addEventListener('click', () => byId('confirm-dialog').close(value));
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

function showMemory(item) {
  selected = item;
  memoryTitle.value = item?.title ?? '';
  memoryTags.value = item?.tags ?? '';
  memoryBody.value = item?.body ?? '';
  byId('editor-title').textContent = item ? '编辑记忆' : '新建记忆';
  byId('editor-detail').textContent = item
    ? `${item.origin === 'automatic' ? '自动记录' : '手动保存'} · 更新于 ${new Date(item.updated).toLocaleString('zh-CN')}`
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

async function refreshMemory() {
  clearTimeout(searchState.timer);
  const sequence = ++searchState.sequence;
  const { items } = await call('list_memories', { query: memorySearch.value });
  if (sequence !== searchState.sequence) return;
  // 若本地没有正在编辑的草稿，列表更新时也更新右侧当前记录。
  const fresh = items.find((item) => item.id === selected?.id);
  searchState.loaded = true;
  searchState.rows = items;
  if (fresh && !memoryChanged()) showMemory(fresh);
  else renderRows();
}

memorySearch.addEventListener('input', () => {
  ++searchState.sequence;
  clearTimeout(searchState.timer);
  searchState.timer = setTimeout(() => perform(refreshMemory), 220);
});
byId('new-memory').addEventListener('click', () => perform(() => selectMemory(null)));
byId('save-memory').addEventListener('click', () => {
  if (!connected || !memoryChanged()) return;
  write(async () => {
    const fields = { title: memoryTitle.value, tags: memoryTags.value, body: memoryBody.value };
    const item = selected
      ? await call('edit_memory', { ...fields, id: selected.id, revision: selected.revision })
      : await call('add_memory', fields);
    showMemory(item);
    await refreshMemory();
    status('记忆已保存。');
  });
});
byId('delete-memory').addEventListener('click', () => perform(async () => {
  if (!selected) return;
  const { id, revision, title } = selected;
  if (!await confirmAction('删除这条记忆？', title, '删除记忆')) return;
  await write(async () => {
    await call('delete_memory', { id, revision });
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

const tabs = [byId('persona-tab'), byId('memory-tab')];
function selectTab(tab) {
  for (const item of tabs) {
    const active = item === tab;
    item.setAttribute('aria-selected', String(active));
    item.tabIndex = active ? 0 : -1;
    byId(item.getAttribute('aria-controls')).hidden = !active;
  }
  status();
  if (connected && tab === tabs[1]) perform(refreshMemory);
}
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const target = tabs[event.key === 'Home' ? 0 : event.key === 'End' ? 1 : 1 - index];
    selectTab(target);
    target.focus();
  });
}

updateControls();
perform(async () => {
  const host = await request('ui/initialize', {
    appInfo: { name: 'dragon-memory', version: '1.1.0' },
    appCapabilities: { availableDisplayModes: ['inline', 'fullscreen'] },
    protocolVersion,
  });
  applyTheme(host?.hostContext);
  post({ method: 'ui/notifications/initialized' });
  connected = true;
  updateControls();
  const [card, settings] = await Promise.all([call('read_persona'), call('get_auto_memory')]);
  showPersona(card);
  autoMemory.checked = settings.enabled;
  autoMemory.disabled = false;
  status();
  if (tabs[1].getAttribute('aria-selected') === 'true' && !searchState.loaded) await refreshMemory();
});
