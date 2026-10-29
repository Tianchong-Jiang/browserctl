const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {spawn, execFileSync} = require('node:child_process');
const scratch = path.join(__dirname, 'scratch');
fs.mkdirSync(scratch, {recursive:true});
const dir = fs.mkdtempSync(path.join(scratch, 'graphical-'));
const log = fs.openSync(path.join(dir, 'desktop.log'), 'w');
const config = JSON.parse(fs.readFileSync(path.join(process.env.XDG_CONFIG_HOME || path.join(require('node:os').homedir(), '.config'), 'browserctl/host.json')));
const display = ':94', port = 19472;
const env = {...process.env, DISPLAY: display, XDG_CONFIG_HOME: dir+'/config', XDG_STATE_HOME: dir+'/state', XDG_CACHE_HOME: dir+'/cache'};
fs.mkdirSync(env.XDG_CONFIG_HOME+'/browserctl', {recursive:true});
config.profile = dir+'/profile';
config.cdp_endpoint = 'http://127.0.0.1:'+port;
config.agent_region = {left:900,top:80,width:850,height:700};
config.human_region = {left:80,top:80,width:800,height:700};
fs.writeFileSync(env.XDG_CONFIG_HOME+'/browserctl/host.json', JSON.stringify(config));
const pause = ms => new Promise(resolve => setTimeout(resolve,ms));
let xephyr, wm, chromePid, ws;
async function main() {
  assert(!fs.existsSync('/tmp/.X11-unix/X94'),'test display in use');
  xephyr = spawn('Xephyr',[display,'-screen','1920x1200','-ac','-nolisten','tcp','-noreset'],{stdio:['ignore',log,log],detached:true});
  for(let i=0;i<100&&!fs.existsSync('/tmp/.X11-unix/X94');i++) await pause(50);
  const outer = execFileSync('xdotool',['search','--sync','--name','Xephyr on :94'],{encoding:'utf8',timeout:3000}).trim().split('\n')[0];
  execFileSync('xdotool',['set_desktop_for_window',outer,'1']);
  wm = spawn('dbus-run-session',['--','gnome-shell','--x11','--display='+display,'--sm-disable'],{env,stdio:['ignore',log,log],detached:true});
  let ready = false;
  for(let i=0;i<100;i++) {
    try {ready = +execFileSync('xdotool',['get_num_desktops'],{env,encoding:'utf8',stdio:['ignore','pipe','ignore']})>=2;if(ready)break;}catch{}
    await pause(100);
  }
  assert(ready,'nested GNOME window manager not ready');
  const run = (...args) => JSON.parse(execFileSync(__dirname+'/browserctl.py',args,{env,encoding:'utf8',timeout:25000}));
  assert.equal(run('start').launched,true);
  let info;
  for(let i=0;i<450;i++) {
    try {info = await (await fetch(config.cdp_endpoint+'/json/version')).json();break;}catch{}
    await pause(100);
  }
  assert(info,'native Chrome did not open its debugging port');
  chromePid = +fs.readlinkSync(config.profile+'/SingletonLock').split('-').pop();
  ws = new WebSocket(info.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  let id=0;
  const pending=new Map();
  ws.onmessage = event => {const m=JSON.parse(event.data),p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p[1](m.error):p[0](m.result);}};
  const send = (method,params={},sessionId) => new Promise((resolve,reject)=>{const n=++id;pending.set(n,[resolve,reject]);ws.send(JSON.stringify({id:n,method,params,sessionId}));});
  const tabs = () => run('tabs');
  const windows = () => new Set(tabs().map(t=>t.windowId));
  const waitTab = async predicate => {
    for(let i=0;i<80;i++) {const t=tabs().find(predicate);if(t)return t;await pause(100);}
    throw new Error('expected tab was not created');
  };
  const human = await waitTab(()=>true);
  assert.equal(windows().size,1);
  const startCount=tabs().length;
  run('start');
  assert.equal(windows().size,1);
  assert.equal(tabs().length,startCount);
  console.log('PASS cold start and repeated icon launch reuse the human window');
  const file = dir+'/test space & # ü.html';
  fs.writeFileSync(file,'<title>Local file test</title><body>LOCAL FILE OK</body>');
  run('start',file);
  const opened = await waitTab(t=>t.url.startsWith('file:') && decodeURIComponent(new URL(t.url).pathname)===file);
  assert.equal(opened.windowId,human.windowId);
  assert.equal(windows().size,1);
  await send('Browser.setWindowBounds',{windowId:human.windowId,bounds:{windowState:'maximized'}});
  const focus = () => execFileSync('xdotool',['getactivewindow'],{env,encoding:'utf8'}).trim();
  const desktop = () => execFileSync('xdotool',['get_desktop'],{env,encoding:'utf8'}).trim();
  await pause(300);
  const previous = focus(), agent = run('prepare');
  assert.equal(focus(),previous);
  assert.equal(execFileSync('xdotool',['get_desktop_for_window',agent.nativeWindowId],{env,encoding:'utf8'}).trim(),'1');
  assert.equal(run('prepare').targetId,agent.targetId);
  const backendNew = url => JSON.parse(execFileSync('/usr/bin/python3',['-c',
    "import browserctl,json,sys; c=browserctl.load_config(); print(json.dumps(browserctl.window_command(c,'new-tab',identity=browserctl.chrome_identity(c),urls=[sys.argv[1]])))",url],
    {cwd:__dirname,env,encoding:'utf8',timeout:20000}));
  async function backgroundCheck(label) {
    const beforeFocus=focus(),beforeDesktop=desktop();
    const eventFile=dir+'/'+label+'-focus.log',fd=fs.openSync(eventFile,'w');
    const spy=spawn('xprop',['-spy','-root','_NET_CURRENT_DESKTOP','_NET_ACTIVE_WINDOW'],{env,stdio:['ignore',fd,log]});
    try {
      await pause(200);
      for(let i=0;i<3;i++) {
        const created=backendNew('about:blank#'+label+'-'+i);
        assert.equal(created.windowId,agent.windowId);
        assert.equal((await send('Browser.getWindowForTarget',{targetId:created.targetId})).windowId,agent.windowId);
      }
      await pause(300);
      assert.equal(focus(),beforeFocus);
      assert.equal(desktop(),beforeDesktop);
    } finally { spy.kill('SIGTERM'); await new Promise(r=>spy.once('exit',r));fs.closeSync(fd); }
    const events=fs.readFileSync(eventFile,'utf8').trim().split('\n');
    assert.equal(events.length,2,'Transient focus or desktop change: '+events.join('\n'));
    console.log('PASS '+label+': three agent tabs, no transient focus/desktop changes');
  }
  await backgroundCheck('human-active');
  await send('Target.activateTarget',{targetId:agent.targetId});
  execFileSync('xdotool',['set_desktop','0'],{env});
  const dummy=spawn('xmessage',['-title','Browserctl Focus Sentinel','Human is working here'],{env,stdio:['ignore',log,log]});
  try {
    const dummyId=execFileSync('xdotool',['search','--sync','--name','^Browserctl Focus Sentinel$'],{env,encoding:'utf8',timeout:3000}).trim().split('\n')[0];
    execFileSync('xdotool',['windowactivate','--sync',dummyId],{env});
    await pause(200);
    await backgroundCheck('agent-last-active');
  } finally {dummy.kill('SIGTERM');}
  await send('Target.activateTarget',{targetId:agent.targetId});
  const file2=dir+'/second.html';fs.writeFileSync(file2,'<title>Second file</title>');
  run('start',file2);
  const second=await waitTab(t=>t.url==='file://'+file2);
  assert.equal(second.windowId,human.windowId);
  assert.equal(windows().size,2);
  assert.equal(desktop(),'0');
  assert.equal((await send('Browser.getWindowForTarget',{targetId:second.targetId})).bounds.windowState,'maximized');
  console.log('PASS human link reuses maximized human window when agent was last active');
  run('start','--new-window','about:blank#explicit-new-window');
  const explicit=await waitTab(t=>t.url==='about:blank#explicit-new-window');
  assert.notEqual(explicit.windowId,human.windowId);
  assert.notEqual(explicit.windowId,agent.windowId);
  await send('Target.closeTarget',{targetId:explicit.targetId});
  console.log('PASS explicit New Window remains available');
  const bindingFile=env.XDG_CONFIG_HOME+'/browserctl/agent-window.json';
  const originalBinding=fs.readFileSync(bindingFile);
  fs.unlinkSync(bindingFile);
  assert.equal(run('prepare').windowId,agent.windowId);
  assert.equal(windows().size,2);
  fs.writeFileSync(bindingFile,'invalid json');
  run('start','about:blank#corrupt-binding');
  assert.equal((await waitTab(t=>t.url==='about:blank#corrupt-binding')).windowId,human.windowId);
  fs.writeFileSync(bindingFile,originalBinding);
  console.log('PASS extension role survives missing/corrupt controller metadata');
  const state=dir+'/state/browserctl';fs.mkdirSync(state,{recursive:true});
  const busy=JSON.stringify({state:'RUNNING',owner:'another-agent'});
  fs.writeFileSync(state+'/task.json',busy);
  const bin=dir+'/bin';fs.mkdirSync(bin);
  fs.writeFileSync(bin+'/systemctl','#!/bin/sh\nexit 99\n',{mode:0o755});env.PATH=bin+':'+env.PATH;
  const entry=dir+'/browserctl-test.desktop',file3=dir+'/gio.html';fs.writeFileSync(file3,'<title>GIO</title>');
  fs.writeFileSync(entry,'[Desktop Entry]\nType=Application\nName=Browserctl Test\nExec='+__dirname+'/browserctl.py start %U\n');
  execFileSync('gio',['launch',entry,file3],{env});
  assert.equal((await waitTab(t=>t.url==='file://'+file3)).windowId,human.windowId);
  assert.equal(fs.readFileSync(state+'/task.json','utf8'),busy);
  console.log('PASS actual desktop-entry link works independently of busy/stopped controller');
  const {sessionId:frozen}=await send('Target.attachToTarget',{targetId:human.targetId,flatten:true});
  await send('ServiceWorker.enable',{},frozen);
  await send('ServiceWorker.stopAllWorkers',{},frozen);
  send('Runtime.evaluate',{expression:'while(true){}'},frozen);
  await pause(150);
  const file4=dir+'/frozen.html';fs.writeFileSync(file4,'<title>Opened while frozen</title>');
  const began=Date.now();run('start',file4);
  assert.equal((await waitTab(t=>t.url==='file://'+file4)).windowId,human.windowId);
  assert(Date.now()-began<5000,'file launch waited on frozen renderer');
  console.log('PASS frozen human renderer and stopped extension worker do not block link opening');
  assert.equal(+fs.readlinkSync(config.profile+'/SingletonLock').split('-').pop(),chromePid);
  ws.close();
  process.kill(chromePid,'SIGTERM');
  for(let i=0;i<100&&fs.existsSync('/proc/'+chromePid);i++)await pause(50);
  const restored=spawn(config.chrome_exe,['--user-data-dir='+config.profile,
    '--remote-debugging-address=127.0.0.1','--remote-debugging-port='+port,
    '--restore-last-session','--no-first-run','--no-default-browser-check'],{env,stdio:['ignore',log,log]});
  chromePid=restored.pid;
  let restartInfo;
  for(let i=0;i<200;i++) {
    try {restartInfo=await(await fetch(config.cdp_endpoint+'/json/version')).json();break;}catch{}
    await pause(100);
  }
  assert(restartInfo,'disposable Chrome restart failed');
  ws=new WebSocket(restartInfo.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  ws.onmessage=event=>{const m=JSON.parse(event.data),p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p[1](m.error):p[0](m.result);}};
  const restoredAgent=await waitTab(t=>t.url==='about:blank#human-active-0');
  const restoredHuman=await waitTab(t=>t.url==='file://'+file2);
  assert.notEqual(restoredAgent.windowId,restoredHuman.windowId);
  run('start','about:blank#after-restart');
  assert.equal((await waitTab(t=>t.url==='about:blank#after-restart')).windowId,restoredHuman.windowId);
  assert.equal(run('prepare').windowId,restoredAgent.windowId);
  assert.equal(windows().size,2);
  console.log('PASS human/agent identity survives Chrome session restore with new window IDs');
  for(const tab of tabs().filter(t=>t.windowId===restoredHuman.windowId))await send('Target.closeTarget',{targetId:tab.targetId});
  run('start','about:blank#new-human');
  assert.notEqual((await waitTab(t=>t.url==='about:blank#new-human')).windowId,restoredAgent.windowId);
  assert.equal(windows().size,2);
  console.log('PASS creates a human window only when none exists');
  console.log('ALL GRAPHICAL CHECKS PASSED');

}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{
  if(ws)ws.close();
  if(!chromePid&&fs.existsSync(config.profile+'/SingletonLock'))chromePid=+fs.readlinkSync(config.profile+'/SingletonLock').split('-').pop();
  if(chromePid)try{process.kill(chromePid,'SIGTERM');}catch{}
  for(const processHandle of [wm,xephyr])if(processHandle)try{process.kill(-processHandle.pid,'SIGTERM');}catch{}
  fs.closeSync(log);console.log('Test artifacts:',dir);
});
