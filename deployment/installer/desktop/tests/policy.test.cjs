const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const p = require('../policy.cjs');
const root = path.resolve('synthetic-installation');
const good = {version: 1, origin: 'https://chief.local', stateRoot: root, installationRoot: root,
  home: root, helper: path.join(root, 'Chief-Installer'), localCA: null};
test('configuration binds an exact HTTPS origin and absolute local paths', () => {
  assert.equal(p.config(good).origin, good.origin);
  for (const origin of ['http://chief.local', 'https://chief.local/path', 'https://x@chief.local',
    'https://chief.local/#x', 'https://chief.local:invalid', 'file:///tmp/x'])
    assert.throws(() => p.config({...good, origin}));
  assert.throws(() => p.config({...good, helper: 'arbitrary-command'}));
  assert.throws(() => p.config({...good, apiKey: 'must-not-enter-shell'}));
});
test('navigation rejects lookalike origins, credentials and other protocols', () => {
  assert.equal(p.sameOrigin('https://chief.local/login', good.origin), true);
  for (const url of ['https://chief.local.evil.test/', 'https://chief.local@evil.test/',
    'https://user@chief.local/', 'javascript:alert(1)', 'file:///etc/passwd', 'http://chief.local'])
    assert.equal(p.sameOrigin(url, good.origin), false);
});
test('service controls cannot introduce shell commands or arbitrary actions', () => {
  assert.deepEqual(p.controlArguments('stop', good), ['--desktop-control', 'stop', '--root', root, '--home', root]);
  for (const action of ['delete', 'stop; rm -rf /', 'install', 'purge']) assert.throws(() => p.controlArguments(action, good));
});
test('close choices require an explicit supported value', () => {
  for (const closeAction of ['ask', 'background', 'stop']) assert.equal(p.preferences({closeAction}).closeAction, closeAction);
  assert.throws(() => p.preferences({closeAction: 'kill everything'}));
  assert.throws(() => p.preferences({closeAction: 'stop', other: true}));
});
test('malformed or different certificates cannot become trusted', () => {
  assert.equal(p.sameCertificate('invalid certificate', Buffer.from('not a certificate')), false);
});
test('remote renderer exposes no Node or native process bridge', () => {
  const source = fs.readFileSync(path.join(__dirname, '../main.cjs'), 'utf8');
  assert.match(source, /app\.enableSandbox\(\)/);
  assert.match(source, /contextIsolation: true/);
  assert.match(source, /nodeIntegration: false/);
  assert.match(source, /webSecurity: true/);
  assert.doesNotMatch(source, /ignore-certificate-errors|no-sandbox|shell\.openExternal|ipcMain/);
});
