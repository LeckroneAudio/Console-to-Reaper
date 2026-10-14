// Command-line wrapper around the DiGiPanion parser for the Reaper Lua script.
// sync_digipanion.py joins: the TextDecoder stand-in below + the parser bundle
// + the page's record-order functions (copyLabelParts ... buildRecordOrder) +
// the run() entry point at the end, and embeds the result in
// DiGiCo_to_Reaper_Full.lua.
//
// Runs under macOS JavaScript for Automation (osascript -l JavaScript, which
// calls run(argv)) or Node.js. Output is TSV for Lua:
//   port<TAB>name<TAB>copies                         one per recorder port, most copies first
//   track<TAB>port<TAB>rec_input<TAB>number<TAB>name<TAB>is_empty<TAB>is_default
//   warn<TAB>text
//   error<TAB>text

// ---- part 1: environment (before the parser) ----
if (typeof TextDecoder === 'undefined') {
  // JavaScriptCore under osascript has no TextDecoder; the parser only decodes windows-1252
  var CP1252 = {128: 8364, 130: 8218, 131: 402, 132: 8222, 133: 8230, 134: 8224, 135: 8225, 136: 710,
                137: 8240, 138: 352, 139: 8249, 140: 338, 142: 381, 145: 8216, 146: 8217, 147: 8220,
                148: 8221, 149: 8226, 150: 8211, 151: 8212, 152: 732, 153: 8482, 154: 353, 155: 8250,
                156: 339, 158: 382, 159: 376};
  globalThis.TextDecoder = function () {};
  TextDecoder.prototype.decode = function (bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(CP1252[bytes[i]] || bytes[i]);
    return s;
  };
}
var window = globalThis;

// ---- part 2: the run() entry point (after the parser and record-order functions) ----
//@@RUN@@
function readSessionBytes(path) {
  if (typeof require === 'function') {
    var file = require('fs').readFileSync(path);
    return file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength);
  }
  // osascript: NSData -> base64 -> bytes (bridging byte by byte would be far slower)
  ObjC.import('Foundation');
  var data = $.NSData.dataWithContentsOfFile(path);
  if (!data || data.isNil()) throw new Error('Could not read ' + path);
  var b64 = data.base64EncodedStringWithOptions(0).js;
  var A = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  var lut = new Uint8Array(256);
  for (var i = 0; i < 64; i++) lut[A.charCodeAt(i)] = i;
  var pad = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0;
  var out = new Uint8Array(b64.length / 4 * 3 - pad);
  for (var k = 0, j = 0; k < b64.length; k += 4) {
    var n = (lut[b64.charCodeAt(k)] << 18) | (lut[b64.charCodeAt(k + 1)] << 12) |
            (lut[b64.charCodeAt(k + 2)] << 6) | lut[b64.charCodeAt(k + 3)];
    if (j < out.length) out[j++] = n >> 16;
    if (j < out.length) out[j++] = (n >> 8) & 255;
    if (j < out.length) out[j++] = n & 255;
  }
  return out.buffer;
}

function run(argv) {
  var clean = function (s) { return String(s).replace(/[\t\r\n]/g, ' '); };
  try {
    var path = argv[0];
    var session = window.digipanionParseSession(readSessionBytes(path), path.split(/[\\/]/).pop());
    var lines = [];
    var ports = copyAudioPorts(session);
    ports.forEach(function (p) {
      lines.push(['port', clean(p.port), p.count].join('\t'));
      var built = buildRecordOrder({ session: session, port: p.port, consoleInputs: [] });
      built.tracks.forEach(function (t) {
        lines.push(['track', clean(p.port), t.rec_input, clean(t.number), clean(t.name),
                    t.is_empty ? 1 : 0, t.is_default ? 1 : 0].join('\t'));
      });
      built.shared.forEach(function (s) {
        lines.push(['warn', clean('More than one channel is copied to ' + s + ' - the first is used.')].join('\t'));
      });
    });
    (session.warnings || []).forEach(function (w) { lines.push(['warn', clean(w)].join('\t')); });
    return lines.join('\n') + '\n';
  } catch (e) {
    return 'error\t' + clean(e && e.message ? e.message : e) + '\n';
  }
}

if (typeof process !== 'undefined' && process.argv && typeof require === 'function') {
  process.stdout.write(run([process.argv[2]]));
}
