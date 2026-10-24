'use strict';
const {app, BrowserWindow, Menu, Tray, nativeImage, dialog, session} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const tls = require('node:tls');
const {randomUUID} = require('node:crypto');
const {execFile} = require('node:child_process');
const {pathToFileURL} = require('node:url');
const policy = require('./policy.cjs');

// No renderer receives Node, preload APIs, process controls or credentials.
app.enableSandbox();
app.setName('Chief Agent');
if (process.platform === 'win32') app.setAppUserModelId('Chief.Agent');
if (process.platform === 'linux' && process.env.WAYLAND_DISPLAY)
  app.commandLine.appendSwitch('ozone-platform', 'wayland');
const args = process.argv.filter(value => value !== '--ozone-platform=wayland')
  .slice(app.isPackaged ? 1 : 2);
const smokeTest = args.at(-1) === '--smoke-test';
if (smokeTest) args.pop();
if (args.length !== 2 || args[0] !== '--chief-config') {
  app.exit(2);
} else {
  let settings;
  try {
    const file = path.resolve(args[1]);
    if (fs.lstatSync(file).isSymbolicLink() || fs.statSync(file).size > 16384) throw new Error();
    settings = policy.config(JSON.parse(fs.readFileSync(file, 'utf8')));
  } catch {
    dialog.showErrorBox('Chief could not open', 'The desktop configuration is missing or invalid. Run Chief Installer to repair it.');
    app.exit(2);
  }
  if (settings) run(settings);
}

function run(settings) {
  const profile = path.join(settings.stateRoot, 'desktop-profile');
  fs.mkdirSync(profile, {recursive: true, mode: 0o700});
  app.setPath('userData', profile);
  app.commandLine.appendSwitch('host-resolver-rules', `MAP ${new URL(settings.origin).hostname} 127.0.0.1`);
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  let window, tray, stopping = false, quitting = false, closing = false;
  const prefFile = path.join(settings.stateRoot, 'desktop-preferences.json');
  let prefs = {closeAction: 'ask', startAtLogin: null};
  try { prefs = policy.preferences(JSON.parse(fs.readFileSync(prefFile, 'utf8'))); } catch {}
  const recovery = pathToFileURL(path.join(__dirname, 'recovery.html')).href;

  function save(action, startup = prefs.startAtLogin) {
    const value = policy.preferences({closeAction: action, startAtLogin: startup});
    const temp = prefFile + '.' + randomUUID() + '.new';
    try {
      fs.writeFileSync(temp, JSON.stringify(value), {mode: 0o600, flag: 'wx'});
      fs.renameSync(temp, prefFile);
    } finally { try { fs.unlinkSync(temp); } catch {} }
    prefs = value;
    menus();
  }

  function control(action) {
    if (smokeTest) return Promise.reject(new Error('Smoke tests cannot control services.'));
    return new Promise((resolve, reject) => {
      execFile(settings.helper, policy.controlArguments(action, settings),
        {windowsHide: true, timeout: 90000, maxBuffer: 16384}, (error, stdout) => {
          if (error) { reject(new Error('Chief could not complete the service request.')); return; }
          try {
            const result = JSON.parse(stdout);
            if (result.status !== 'SUCCESS') throw new Error();
            resolve(result);
          } catch { reject(new Error('Chief could not confirm the service state.')); }
        });
    });
  }

  async function stopAndQuit() {
    if (stopping) return;
    stopping = true;
    window?.setTitle('Chief Agent — stopping background work…');
    try {
      await control('stop');
      quitting = true;
      app.quit();
    } catch {
      window?.show();
      await dialog.showMessageBox(window, {type: 'error', title: 'Chief is still open',
        message: 'Background services could not be confirmed stopped.',
        detail: 'Chief has stayed open. Use Start or stop background work in the Chief menu to retry.', buttons: ['OK']});
    } finally { stopping = false; window?.setTitle('Chief Agent'); }
  }

  async function close() {
    if (closing) return;
    closing = true;
    try {
    let action = prefs.closeAction;
    if (action === 'ask') {
      const choice = await dialog.showMessageBox(window, {type: 'question', title: 'Close Chief',
        message: 'What should happen when you close Chief?',
        detail: 'Keeping Chief running lets schedules and notifications continue. Stopping Chief makes its local server and connected PWAs unavailable until you start it again.',
        buttons: ['Keep running in background', 'Stop Chief completely', 'Cancel'],
        defaultId: 2, cancelId: 2, checkboxLabel: 'Remember my choice', checkboxChecked: false});
      if (choice.response === 2) return;
      action = choice.response === 0 ? 'background' : 'stop';
      if (choice.checkboxChecked) save(action);
    }
    if (action === 'stop') await stopAndQuit();
    else if (tray) window.hide();
    else await dialog.showMessageBox(window, {message: 'This desktop does not provide a tray. Minimize Chief to keep its controls accessible.', buttons: ['OK']});
    } finally { closing = false; }
  }

  function menus() {
    const items = [
      {label: 'Open Chief', click: () => { window.show(); window.focus(); }},
      {label: 'Reconnect', click: load},
      {label: 'Start background work', click: async () => {
        try { await control('start'); load(); }
        catch { dialog.showErrorBox('Chief could not start', 'Use the installer repair option or retry from the Chief menu.'); }
      }},
      {label: 'When closing Chief', submenu: [
        ['ask', 'Ask each time'], ['background', 'Keep running in background'], ['stop', 'Stop Chief completely']
      ].map(([value, label]) => ({label, type: 'radio', checked: prefs.closeAction === value, click: () => save(value)}))},
      {label: 'Start background services automatically', submenu: [true, false].map(enabled => ({
        label: enabled ? 'Enabled' : 'Disabled', type: 'radio', checked: prefs.startAtLogin === enabled,
        click: async () => {
          try {
            await control(enabled ? 'startup-enable' : 'startup-disable');
            save(prefs.closeAction, enabled);
          } catch { dialog.showErrorBox('Startup preference was not saved', 'Chief could not confirm the service setting. Retry from the Chief menu.'); }
        }
      }))},
      {type: 'separator'},
      {label: 'Stop Chief completely', click: stopAndQuit},
    ];
    Menu.setApplicationMenu(Menu.buildFromTemplate([{label: 'Chief', submenu: items},
      {label: 'View', submenu: [{role: 'resetZoom'}, {role: 'zoomIn'}, {role: 'zoomOut'}, {role: 'togglefullscreen'}]}]));
    tray?.setContextMenu(Menu.buildFromTemplate(items));
  }

  function load() {
    return window.loadURL(settings.origin + '/').catch(error => {
      if (smokeTest) throw error;
      return window.loadFile(path.join(__dirname, 'recovery.html'));
    });
  }

  function verifyLocalCertificate(request, callback) {
    if (!settings.localCA || request.hostname !== new URL(settings.origin).hostname) { callback(-3); return; }
    if (request.errorCode === 0) { callback(-3); return; }
    // Validate the full installed local-CA chain, hostname and expiry in TLS,
    // then require the identical leaf certificate Chromium actually received.
    // A matching hostname alone is never permission to bypass verification.
    let socket, answered = false;
    const answer = (code) => { if (!answered) { answered = true; socket?.destroy(); callback(code); } };
    try {
      socket = tls.connect({host: '127.0.0.1', port: Number(new URL(settings.origin).port || 443), servername: request.hostname,
        ca: fs.readFileSync(settings.localCA), rejectUnauthorized: true}, () => {
          const raw = socket.getPeerCertificate().raw;
          answer(socket.authorized && policy.sameCertificate(request.certificate.data, raw) ? 0 : -2);
        });
      socket.setTimeout(3000, () => answer(-2));
      socket.on('error', error => {
        if (smokeTest) console.error('Local TLS verification:', error.code);
        answer(-2);
      });
    } catch (error) {
      if (smokeTest) console.error('Certificate verification error:', error.message);
      answer(-2);
    }
  }

  app.on('second-instance', () => { window?.show(); window?.focus(); });
  app.on('window-all-closed', () => {});
  app.on('before-quit', event => {
    if (!quitting) { event.preventDefault(); stopAndQuit(); }
  });
  app.whenReady().then(() => {
    const isolated = session.fromPartition('persist:chief-desktop');
    isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    isolated.setPermissionCheckHandler(() => false);
    isolated.setCertificateVerifyProc(verifyLocalCertificate);
    isolated.webRequest.onBeforeRequest((details, callback) => callback({cancel:
      !policy.sameOrigin(details.url, settings.origin) && details.url !== recovery}));
    window = new BrowserWindow({width: 1280, height: 850, minWidth: 720, minHeight: 480,
      title: 'Chief Agent', show: false, webPreferences: {session: isolated, sandbox: true,
        contextIsolation: true, nodeIntegration: false, webviewTag: false, webSecurity: true,
        allowRunningInsecureContent: false, devTools: false}});
    window.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
    window.webContents.on('will-attach-webview', event => event.preventDefault());
    window.webContents.on('will-navigate', (event, url) => {
      if (!policy.sameOrigin(url, settings.origin) && url !== recovery) event.preventDefault();
    });
    window.webContents.on('will-redirect', (event, url) => {
      if (!policy.sameOrigin(url, settings.origin)) event.preventDefault();
    });
    window.on('close', event => { if (!quitting) { event.preventDefault(); close(); } });
    if (!smokeTest) window.once('ready-to-show', () => window.show());
    else {
      setTimeout(() => {console.error('Desktop smoke test timed out'); app.exit(1);}, 30000).unref();
    }
    async function verifySmokeLoad() {
        try {
          // Error documents can emit did-finish-load with the requested HTTPS
          // URL. Only a resolved loadURL establishes a successful navigation.
          await load();
          if (!policy.sameOrigin(window.webContents.getURL(), settings.origin)) throw new Error();
          const secure = await window.webContents.executeJavaScript(
            "window.isSecureContext && typeof require === 'undefined' && typeof process === 'undefined'");
          if (!secure) throw new Error();
          console.log(JSON.stringify({status: 'SUCCESS', chromium: process.versions.chrome,
            electron: process.versions.electron, nodeExposed: false}));
          quitting = true; app.quit();
        } catch {console.error('Desktop smoke test failed'); app.exit(1);}
    }
    try {
      const icon = nativeImage.createFromPath(path.join(__dirname, 'icon.png'));
      if (smokeTest) throw new Error('No tray in hidden acceptance tests');
      tray = new Tray(icon);
      tray.setToolTip('Chief Agent');
      tray.on('double-click', () => {window.show(); window.focus();});
    } catch { tray = null; }
    menus();
    if (smokeTest) verifySmokeLoad();
    else {
      window.loadFile(path.join(__dirname, 'recovery.html')).then(async () => {
        window.setTitle('Chief Agent — checking background services…');
        try {
          const status = await control('status');
          if (!status.active || Object.values(status.active).some(value => value !== true)) {
            window.setTitle('Chief Agent — starting background services…');
            await control('start');
          }
          await load();
        } catch {
          // Recovery remains visible; no external browser or silent success.
        } finally { window.setTitle('Chief Agent'); }
      });
    }
  });
}
