// Browser-level routing is independent of MCP and of page renderer responsiveness.
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {randomUUID} = require('node:crypto');
const {config, identity, mode, bindingFile, urls, newWindow} = JSON.parse(fs.readFileSync(0, 'utf8'));
let socket;
async function main() {
  const version = await (await fetch(config.cdp_endpoint + '/json/version')).json();
  socket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let next = 0;
  const pending = new Map();
  socket.onmessage = event => {
    const message = JSON.parse(event.data), callback = pending.get(message.id);
    if (callback) {
      pending.delete(message.id);
      message.error ? callback.reject(new Error(JSON.stringify(message.error))) : callback.resolve(message.result);
    }
  };
  socket.onclose = () => {
    for (const p of pending.values()) p.reject(new Error('Chrome connection closed'));
  };
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const id = ++next;
    pending.set(id, {resolve, reject});
    socket.send(JSON.stringify({id, method, params, sessionId}));
  });
  const attach = async targetId => (await send('Target.attachToTarget', {targetId, flatten: true})).sessionId;
  const targets = async () => (await send('Target.getTargets')).targetInfos;
  const pages = async () => (await targets()).filter(t => t.type === 'page');
  if (mode === 'check') {
    const stale = [];
    for (const t of await pages()) {
      const session = await attach(t.targetId);
      if ((await send('Page.getFrameTree', {}, session)).frameTree.frame.id !== t.targetId) stale.push(t.url);
      await send('Target.detachFromTarget', {sessionId: session});
    }
    return stale;
  }
  const tabs = [];
  for (const t of await pages()) {
    try { tabs.push({...t, ...(await send('Browser.getWindowForTarget', {targetId: t.targetId}))}); }
    catch (e) { if (!e.message.includes('Browser window not found')) throw e; }
  }
  if (mode === 'tabs') return tabs.map(({targetId, windowId, title, url}) => ({targetId, windowId, title, url}));
  const extensionPath = path.join(__dirname, 'extension');
  const installed = (await send('Extensions.getExtensions')).extensions.find(e => e.path === extensionPath);
  if (installed && !installed.enabled) throw new Error('Browserctl routing extension is disabled');
  const extensionId = installed?.id || (await send('Extensions.loadUnpacked', {path: extensionPath})).id;
  const scopeURL = `chrome-extension://${extensionId}/`;
  let worker = (await targets()).find(t => t.type === 'service_worker' && t.url === scopeURL + 'worker.js');
  if (!worker) {
    if (!tabs.length) throw new Error('No browser tab available to wake the routing extension');
    const sessionId = await attach(tabs[0].targetId);
    try {
      await send('ServiceWorker.enable', {}, sessionId);
      await send('ServiceWorker.startWorker', {scopeURL}, sessionId);
    }
    finally { await send('Target.detachFromTarget', {sessionId}); }
    for (let i = 0; i < 40 && !worker; i++) {
      worker = (await targets()).find(t => t.type === 'service_worker' && t.url === scopeURL + 'worker.js');
      if (!worker) await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  if (!worker) throw new Error('Routing extension worker did not start');
  const workerSession = await attach(worker.targetId);
  let ready = false;
  for (let i = 0; i < 40 && !ready; i++) {
    ready = (await send('Runtime.evaluate', {expression: 'typeof browserctl'}, workerSession)).result.value === 'function';
    if (!ready) await new Promise(resolve => setTimeout(resolve, 50));
  }
  if (!ready) throw new Error('Routing extension did not initialize');
  const route = async args => {
    const result = await send('Runtime.evaluate', {expression: `browserctl(${JSON.stringify(args)})`,
      awaitPromise: true, returnByValue: true}, workerSession);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  };
  let agentWindow = await route({action: 'agent-window'});
  let binding = null;
  if (fs.existsSync(bindingFile)) {
    try { binding = JSON.parse(fs.readFileSync(bindingFile, 'utf8')); }
    catch (error) { if (!agentWindow) throw error; }
  }
  if (binding && (binding.pid !== identity.pid || binding.start !== identity.start)) binding = null;
  // Adopt the existing verified window when upgrading installations without tab groups.
  if (!agentWindow && binding && tabs.some(t => t.windowId === binding.windowId))
    agentWindow = await route({action: 'register-agent', windowId: binding.windowId});
  if (mode === 'human') return route({action: 'human', urls, newWindow,
    region: config.hostname === 'laptop' ? {state: 'maximized'} : config.human_region});
  const save = () => {
    fs.writeFileSync(bindingFile + '.tmp', JSON.stringify(binding), {mode: 0o600});
    fs.renameSync(bindingFile + '.tmp', bindingFile);
  };
  const navigate = async (targetId, url) => {
    const session = await attach(targetId);
    try {
      const result = await send('Page.navigate', {url}, session);
      if (result.errorText) throw new Error(result.errorText);
    } finally { await send('Target.detachFromTarget', {sessionId: session}); }
  };
  if (mode === 'new-tab') {
    if (!binding || binding.marker || binding.windowId !== agentWindow)
      throw new Error('Agent window is missing or changed; run prepare');
    const marker = 'about:blank#browserctl-' + randomUUID();
    await route({action: 'new-tab', windowId: binding.windowId, urls: [marker]});
    const matches = (await pages()).filter(t => t.url === marker);
    if (matches.length !== 1) throw new Error('Cannot identify newly created agent tab; do not replay');
    const {targetId} = matches[0];
    const {windowId} = await send('Browser.getWindowForTarget', {targetId});
    if (windowId !== binding.windowId) throw new Error('New tab opened in the wrong window');
    await navigate(targetId, urls[0]);
    return {targetId, windowId};
  }
  if (mode !== 'agent') throw new Error('Unknown window command: ' + mode);
  let tab = tabs.find(t => t.targetId === binding?.targetId && t.windowId === binding.windowId);
  if (!tab) {
    const parent = tabs.find(t => t.windowId === (agentWindow || binding?.windowId));
    const marker = 'browserctl-' + randomUUID();
    tab = parent || await send('Target.createTarget', {url: 'data:text/html,<title>' + marker + '</title>',
      newWindow: true, background: true, focus: false, ...config.agent_region});
    binding = {...identity, ...tab, ...(await send('Browser.getWindowForTarget', {targetId: tab.targetId})),
      marker: parent ? null : marker};
    save();
  }
  if (binding.marker) {
    if (config.hostname === 'laptop') {
      let ids;
      for (let attempt = 0;; attempt++) {
        try {
          ids = execFileSync('xdotool', ['search', '--all', '--onlyvisible', '--sync', '--pid', String(identity.pid), '--name', binding.marker],
            {encoding: 'utf8', timeout: 5000, stdio: ['ignore', 'pipe', 'pipe']}).trim().split('\n');
          break;
        } catch (e) { if (attempt === 4 || !String(e.stderr).includes('BadWindow')) throw e; }
      }
      if (ids.length !== 1) throw new Error('New agent desktop window is ambiguous: ' + ids);
      execFileSync('xdotool', ['set_desktop_for_window', ids[0], '1']);
      binding.nativeWindowId = ids[0];
    }
    await navigate(binding.targetId, 'about:blank');
    delete binding.marker;
    save();
  }
  await route({action: 'register-agent', windowId: binding.windowId});
  return binding;
}
main().then(result => console.log(JSON.stringify(result))).catch(error => {
  console.error(error.message); process.exitCode = 1;
}).finally(() => socket?.close());
