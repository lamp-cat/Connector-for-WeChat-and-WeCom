'use strict';
function report(payload) {
  send(['frida:stdout', JSON.stringify(payload) + '\n']);
}
const seen = new Set();
const hooked = new Set();
const contexts = new Map();
const calls = {};
let installed = 0;
function count(name, length) {
  const id = name + ':' + length;
  if (Object.keys(calls).length < 80 || calls[id]) calls[id] = (calls[id] || 0) + 1;
}
function candidate(bytes) {
  if (bytes.length !== 24 || bytes[20] !== 115 || bytes[21] !== 65 || bytes[22] !== 108 || bytes[23] !== 84) return;
  let raw = '';
  for (let i = 0; i < 16; i++) raw += bytes[i].toString(16).padStart(2, '0');
  if (!seen.has(raw)) { seen.add(raw); report({type: 'candidate', value: raw}); }
}
function read(data, length) {
  return Array.from(new Uint8Array(data.readByteArray(length)));
}
function hook(address, callbacks) {
  if (!address || hooked.has(address.toString())) return;
  hooked.add(address.toString());
  Interceptor.attach(address, callbacks);
  installed++;
}
function stream(prefix, init, update, final, piece = false, finalContext = 1) {
  hook(init, {onEnter(args) {
    if (contexts.size > 4096) contexts.clear();
    contexts.set(prefix + args[0], []);
  }});
  hook(update, {onEnter(args) {
    try {
      const id = prefix + args[0];
      const data = piece ? args[1].readPointer() : args[1];
      const length = piece ? args[1].add(Process.pointerSize).readU64().toNumber() : args[2].toUInt32();
      count(prefix, length);
      if (!contexts.has(id)) return;
      const bytes = contexts.get(id);
      if (length > 24 || bytes.length + length > 24) { contexts.delete(id); return; }
      bytes.push(...read(data, length));
    } catch (_) {}
  }});
  hook(final, {onEnter(args) {
    const id = prefix + args[finalContext];
    const bytes = contexts.get(id);
    if (bytes) candidate(bytes);
    contexts.delete(id);
  }});
}
function resolve(name) { return Module.findGlobalExportByName(name); }
for (const name of ['CC_MD5', 'MD5']) {
  hook(resolve(name), {onEnter(args) {
    try {
      const length = args[1].toUInt32();
      count(name, length);
      if (length === 24) candidate(read(args[0], 24));
    } catch (_) {}
  }});
}
stream('cc:', resolve('CC_MD5_Init'), resolve('CC_MD5_Update'), resolve('CC_MD5_Final'));
stream('ssl:', resolve('MD5_Init'), resolve('MD5_Update'), resolve('MD5_Final'));
for (const m of Process.enumerateModules()) {
  if (!m.path.includes('企业微信.app') && m.name !== 'capture-fixture') continue;
  const exports = m.enumerateExports();
  const find = pattern => (exports.find(x => pattern.test(x.name)) || {}).address;
  const exact = name => (exports.find(x => x.name === name) || {}).address;
  stream(m.name + ':native:', exact('md5_init'), exact('md5_process'), exact('md5_finish'), false, 0);
  stream(m.name + ':openssl:', exact('MD5_Init'), exact('MD5_Update'), exact('MD5_Final'));
  stream(m.name + ':', find(/^_ZN4base7MD5Init/), find(/^_ZN4base9MD5Update/), find(/^_ZN4base8MD5Final/), true);
  hook(find(/^_ZN4base6MD5Sum/), {onEnter(args) {
    try {
      if (args[1].toUInt32() === 24) candidate(read(args[0], 24));
    } catch (_) {}
  }});
}
report({type: 'installed', count: installed, pid: Process.id});
setInterval(() => { report({type: 'activity', calls}); }, 10000);
