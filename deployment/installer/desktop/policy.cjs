'use strict';
const path = require('node:path');
const {X509Certificate} = require('node:crypto');

function config(value) {
  const fields = ['version', 'origin', 'stateRoot', 'installationRoot', 'home', 'helper', 'localCA'];
  if (!value || Object.keys(value).sort().join() !== fields.sort().join() || value.version !== 1)
    throw new Error('Invalid desktop configuration');
  const url = new URL(value.origin);
  if (url.protocol !== 'https:' || url.username || url.password || url.hash || url.search ||
      url.pathname !== '/' || !/^[a-z0-9][a-z0-9.-]*$/i.test(url.hostname) ||
      value.origin !== url.origin) throw new Error('Chief requires its exact HTTPS origin');
  for (const key of ['stateRoot', 'installationRoot', 'home', 'helper'])
    if (typeof value[key] !== 'string' || !path.isAbsolute(value[key]) || /[\x00-\x1f]/.test(value[key]))
      throw new Error('Desktop paths must be absolute');
  if (value.localCA !== null && (typeof value.localCA !== 'string' || !path.isAbsolute(value.localCA)))
    throw new Error('Invalid local CA path');
  return Object.freeze({...value});
}

function sameOrigin(address, origin) {
  try {
    const url = new URL(address);
    return url.protocol === 'https:' && url.origin === origin && !url.username && !url.password;
  } catch { return false; }
}

function preferences(value) {
  if (!value || Object.keys(value).some(key => !['closeAction', 'startAtLogin'].includes(key)) ||
      !['ask', 'background', 'stop'].includes(value.closeAction)) throw new Error('Invalid close preference');
  if (value.startAtLogin !== undefined && value.startAtLogin !== null && typeof value.startAtLogin !== 'boolean')
    throw new Error('Invalid startup preference');
  return {closeAction: value.closeAction, startAtLogin: value.startAtLogin ?? null};
}

function sameCertificate(pem, raw) {
  try { return new X509Certificate(pem).raw.equals(raw); } catch { return false; }
}

function controlArguments(action, settings) {
  if (!['start', 'stop', 'status', 'startup-enable', 'startup-disable'].includes(action)) throw new Error('Invalid service action');
  return ['--desktop-control', action, '--root', settings.installationRoot, '--home', settings.home];
}

module.exports = {config, sameOrigin, preferences, sameCertificate, controlArguments};
