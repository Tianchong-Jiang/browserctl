// Window roles live in a named tab group, so session restore preserves them.
const agentTitle = 'Agent browser';
async function agentGroup() {
  const groups = await chrome.tabGroups.query({title: agentTitle});
  if (groups.length > 1) throw new Error('Multiple agent groups; window identity is ambiguous');
  return groups[0];
}
chrome.windows.onFocusChanged.addListener(async windowId => {
  if (windowId < 0 || windowId === (await agentGroup())?.windowId) return;
  const window = await chrome.windows.get(windowId).catch(() => null);
  if (window?.type === 'normal' && !window.incognito)
    await chrome.storage.session.set({humanWindowId: windowId});
});
async function browserctl({action, windowId, urls = [], newWindow = false, region}) {
  const group = await agentGroup();
  if (action === 'agent-window') return group?.windowId ?? null;
  if (action === 'register-agent') {
    if (group && group.windowId !== windowId) throw new Error('Another agent window is already registered');
    const tabs = await chrome.tabs.query({windowId});
    const groupId = await chrome.tabs.group({tabIds: tabs.map(tab => tab.id),
      ...(group ? {groupId: group.id} : {createProperties: {windowId}})});
    await chrome.tabGroups.update(groupId, {title: agentTitle, color: 'grey'});
    return windowId;
  }
  if (action === 'new-tab') {
    if (windowId !== group?.windowId) throw new Error('Agent window identity does not match its tab group');
    const tab = await chrome.tabs.create({windowId, url: urls[0], active: false});
    await chrome.tabs.group({tabIds: [tab.id], groupId: group.id});
    return tab;
  }
  if (action !== 'human') throw new Error('Unknown tab action: ' + action);
  const windows = (await chrome.windows.getAll({windowTypes: ['normal']}))
    .filter(window => !window.incognito && window.id !== group?.windowId);
  const {humanWindowId} = await chrome.storage.session.get('humanWindowId');
  const human = windows.find(window => window.focused) ||
    windows.find(window => window.id === humanWindowId) || windows[0];
  if (!human || newWindow) {
    const window = await chrome.windows.create({url: urls.length ? urls : ['chrome://newtab/'],
      focused: true, ...region});
    return {windowId: window.id};
  }
  for (const url of urls) await chrome.tabs.create({windowId: human.id, url, active: true});
  await chrome.windows.update(human.id, {focused: true});
  return {windowId: human.id};
}
