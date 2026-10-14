#!/usr/bin/env python3
"""Regression tests for Console to Reaper — run before cutting a release.

    python3 tests/run_tests.py

Stdlib only. Tests that need optional extras (lupa for Lua compilation,
local .ses/.rtf fixture files) are skipped with a note when unavailable.

Covers:
  - OSC fetch engine vs a mock console: iPad command set (counts, stereo
    modes, console/session names), generic /sd set (names only),
    fail-fast on an unreachable console
  - /osc_fetch HTTP endpoint: JSON, TSV, and error paths
  - embedded fetcher in DiGiCo_OSC_to_Reaper.lua is byte-identical to
    digico_osc_fetch.py (sync invariant)
  - all Lua scripts compile (needs lupa)
  - Reaper track template generation (mono / split / keep-stereo routing)
  - .ses parser vs RTF session-report ground truth (needs local fixtures)
  - DiGiPanion Copy Audio parser: browser bundle in sync with the source,
    served at /digipanion.js, same result as the Node CLI, and the Raleigh
    Waves patch (needs node + local fixture)
  - template inputs fixed by the Copy Audio record order (rec_input)
  - stereo flags vs the RTF report, and the newer vO session format
    (125-byte records): app parser and Lua parser must match DiGiPanion
  - Lua Copy Audio record order: embedded helper in sync, and the whole
    Lua scripts (Full and the one-step Copy Audio script) run in a stand-in
    Reaper (lupa) create Waves 1-60 on their own inputs (macOS osascript path)
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.request
from http.server import HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # so the app finds digipanion_bundle, as it does when run from ROOT

# fixture locations (skipped gracefully when absent)
SES_FIXTURE = '/Users/michaelleckrone/Desktop/Console-to-Reaper Test Files/DiGiCo/spokane.ses'
RTF_FIXTURE = '/Users/michaelleckrone/Desktop/Console-to-Reaper Test Files/DiGiCo/spokane.rtf'
COPY_AUDIO_FIXTURE = os.path.join(ROOT, 'raleigh.ses')
NO_COPY_FIXTURE = os.path.join(ROOT, 'app test 2.ses')   # session with no Copy Audio patch

RESULTS = []


def report(name, ok, note=''):
    RESULTS.append((name, ok, note))
    mark = 'PASS' if ok else ('SKIP' if ok is None else 'FAIL')
    print(f'  [{mark}] {name}' + (f' — {note}' if note else ''))


def load_app():
    spec = importlib.util.spec_from_file_location(
        'app', os.path.join(ROOT, 'console_to_reaper.py'))
    mod = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- OSC mock

def pad(b):
    return b + b'\x00' * ((4 - len(b) % 4) % 4)


def s_msg(a, v):
    return pad(a.encode() + b'\x00') + pad(b',s\x00') + pad(v.encode() + b'\x00')


def i_msg(a, v):
    return pad(a.encode() + b'\x00') + pad(b',i\x00') + struct.pack('>i', v)


def ia_msg(a, vals):
    return (pad(a.encode() + b'\x00') + pad((',' + 'i' * len(vals)).encode() + b'\x00') +
            b''.join(struct.pack('>i', v) for v in vals))


def ipad_db(prefix=''):
    """Mock console DB. prefix='/sd' turns it into a generic-set console
    (names only, /sd-prefixed replies)."""
    db = {}
    names = {'Input_Channels': ('Channel_Input/name', ['Kick In', 'Ch 2', 'Guest Verb']),
             'Aux_Outputs': ('Buss_Trim/name', ['EVAN', 'ASL']),
             'Group_Outputs': ('Buss_Trim/name', ['Master']),
             'Matrix_Outputs': ('Buss_Trim/name', ['Cue L'])}
    for sec, (leaf, lst) in names.items():
        for n, nm in enumerate(lst, 1):
            db[f'/{sec}/{n}/{leaf}/?'] = [s_msg(f'{prefix}/{sec}/{n}/{leaf}', nm)]
    if not prefix:  # console-level answers exist only on the iPad set
        db['/Console/Name/?'] = [s_msg('/Console/Name', 'SD7Q-Q3')]
        db['/Console/Session/Filename/?'] = [s_msg('/Console/Session/Filename', 'test.ses')]
        db['/Console/Channels/?'] = [
            i_msg('/Console/Input_Channels', 3), i_msg('/Console/Aux_Outputs', 2),
            i_msg('/Console/Group_Outputs', 1), i_msg('/Console/Matrix_Outputs', 1)]
        db['/Console/Input_Channels/modes/?'] = [ia_msg('/Console/Input_Channels/modes', [1, 1, 2])]
        db['/Console/Aux_Outputs/modes/?'] = [ia_msg('/Console/Aux_Outputs/modes', [2, 1])]
        db['/Console/Group_Outputs/modes/?'] = [ia_msg('/Console/Group_Outputs/modes', [2])]
    return db


class MockConsole:
    """UDP responder that answers '/?' queries and flags any setter."""

    def __init__(self, db, console_rx, client_rx):
        self.db, self.client_rx = db, client_rx
        self.setters = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', console_rx))
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                q = data[:data.index(b'\x00')].decode()
            except Exception:
                continue
            if not q.endswith('/?'):
                self.setters.append(q)
            for pkt in self.db.get(q, []):
                self.sock.sendto(pkt, ('127.0.0.1', self.client_rx))

    def close(self):
        self.stop.set()
        self.sock.close()


# ---------------------------------------------------------------- tests

def test_osc_engine(app):
    mock = MockConsole(ipad_db(), 18202, 18201)
    try:
        parsed, cn, sn = app.fetch_digico_osc('127.0.0.1', 18202, 18201)
        nums = {k: [c['number'] for c in v] for k, v in parsed.items()}
        ok = (cn == 'SD7Q-Q3' and sn == 'test.ses'
              and nums['inputs'] == ['CH1', 'CH2', 'CH3s']
              and nums['aux'] == ['AUX1s', 'AUX2']
              and nums['groups'] == ['GRP1s']
              and nums['matrix'] == ['MTX1']
              and parsed['inputs'][1]['is_default'] is True
              and not mock.setters)
        report('OSC engine, iPad set (counts/modes/names/stereo)', ok,
               '' if ok else f'{cn!r} {sn!r} {nums} setters={mock.setters}')
    finally:
        mock.close()

    mock = MockConsole(ipad_db(prefix='/sd'), 18212, 18211)
    try:
        parsed, cn, sn = app.fetch_digico_osc('127.0.0.1', 18212, 18211)
        total = sum(len(v) for v in parsed.values())
        stereo = sum(1 for v in parsed.values() for c in v if c['number'].endswith('s'))
        ok = total == 7 and stereo == 0 and cn == '' and not mock.setters
        report('OSC engine, generic /sd set (names only)', ok,
               '' if ok else f'total={total} stereo={stereo}')
    finally:
        mock.close()

    t0 = time.time()
    parsed, _, _ = app.fetch_digico_osc('127.0.0.1', 19999, 19989)
    dt = time.time() - t0
    ok = sum(len(v) for v in parsed.values()) == 0 and dt < 2.5
    report('OSC engine, unreachable console fails fast', ok, f'{dt:.1f}s')


def test_http_endpoint(app):
    mock = MockConsole(ipad_db(), 18222, 18221)
    srv = HTTPServer(('127.0.0.1', 18299), app.DiGiCoToReaperHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def post(payload):
            req = urllib.request.Request(
                'http://127.0.0.1:18299/osc_fetch', data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'})
            return urllib.request.urlopen(req).read().decode()

        r = json.loads(post({'ip': '127.0.0.1', 'send_port': 18222, 'listen_port': 18221}))
        ok = (r['success'] and r['source'] == 'SD7Q-Q3 — test.ses'
              and r['counts'] == {'inputs': 3, 'aux': 2, 'groups': 1, 'matrix': 1}
              and r['sections']['inputs'][2]['number'] == 'CH3s')
        report('/osc_fetch JSON (browser path)', ok, '' if ok else str(r)[:100])

        t = post({'ip': '127.0.0.1', 'send_port': 18222, 'listen_port': 18221,
                  'format': 'tsv'})
        lines = t.strip().split('\n')
        ok = (lines[0] == 'meta\t0\tSD7Q-Q3\t0\t0'
              and 'inputs\t3\tGuest Verb\t0\t1' in lines
              and 'aux\t1\tEVAN\t0\t1' in lines)
        report('/osc_fetch TSV (Reaper path)', ok, '' if ok else t[:120])

        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(('127.0.0.1', 18221))
        try:
            t = post({'ip': '127.0.0.1', 'send_port': 18222, 'listen_port': 18221,
                      'format': 'tsv'})
            ok = t.startswith('error\t1\t') and 'Companion' in t
            report('/osc_fetch TSV error line on port conflict', ok, '' if ok else t[:100])
        finally:
            blocker.close()
    finally:
        srv.shutdown()
        mock.close()


def test_embedded_sync():
    lua_text = open(os.path.join(ROOT, 'Lua Scripts', 'DiGiCo_OSC_to_Reaper.lua')).read()
    m = re.search(r'local PYFETCH = \[==\[\n(.*?)\]==\]', lua_text, re.S)
    canon = open(os.path.join(ROOT, 'digico_osc_fetch.py')).read()
    ok = bool(m) and m.group(1) == canon
    report('embedded fetcher identical to digico_osc_fetch.py', ok,
           '' if ok else 'run: python3 sync_embedded_fetcher.py')


def test_lua_compiles():
    try:
        import lupa
    except ImportError:
        report('Lua scripts compile', None, 'lupa not installed — skipped')
        return
    lua = lupa.LuaRuntime(encoding=None)
    check = lua.eval(b'function(src) local f, e = load(src) '
                     b'if f then return "" else return e end end')
    bad = []
    lua_dir = os.path.join(ROOT, 'Lua Scripts')
    for f in sorted(os.listdir(lua_dir)):
        if f.endswith('.lua'):
            err = check(open(os.path.join(lua_dir, f), 'rb').read())
            if err:
                bad.append(f'{f}: {err.decode()[:60]}')
    report('Lua scripts compile', not bad, '; '.join(bad))


def test_track_template(app):
    channels = [
        {'number': 'CH1', 'name': 'Kick', 'color': None},
        {'number': 'CH2s', 'name': 'Verb', 'color': None},
        {'number': 'CH3', 'name': 'Vox', 'color': None},
    ]
    split = app.generate_reaper_track_template(channels, 'split')
    keep = app.generate_reaper_track_template(channels, 'stereo')
    ok_split = ('Verb L' in split and 'Verb R' in split
                and split.count('<TRACK') == 4)
    # keep-stereo: one track for CH2s with stereo input pair 1024+1=1025,
    # and Vox lands on hw input 3 (kick 0, verb 1+2)
    ok_keep = ('Verb L' not in keep and keep.count('<TRACK') == 3
               and '1025' in keep)
    report('track template: split stereo to L/R', ok_split)
    report('track template: keep stereo (paired input)', ok_keep)


def test_ses_parser(app):
    if not (os.path.exists(SES_FIXTURE) and os.path.exists(RTF_FIXTURE)):
        report('.ses parser vs RTF ground truth', None, 'fixture files not found — skipped')
        return
    with open(RTF_FIXTURE, 'rb') as f:
        with contextlib.redirect_stdout(io.StringIO()):
            rtf = app.parse_digico_rtf(f.read().decode('latin-1'))
    with open(SES_FIXTURE, 'rb') as f:
        with contextlib.redirect_stdout(io.StringIO()):
            ses = app.parse_ses_show_file(f.read())
    bad = []
    for key in ('inputs', 'aux', 'groups', 'matrix'):
        want = [c['name'].rstrip() for c in rtf[key]]
        got = [c['name'] for c in ses[key]]
        if want != got:
            diffs = [i + 1 for i in range(min(len(want), len(got)))
                     if want[i] != got[i]]
            bad.append(f'{key}: {len(diffs) or abs(len(want)-len(got))} mismatches')
    report('.ses parser vs RTF ground truth', not bad, '; '.join(bad))
    bad = []
    for key in SECTIONS:
        want = [c['number'].endswith('s') for c in rtf[key]]
        got = [c['number'].endswith('s') for c in ses[key]]
        if want != got:
            bad.append(f'{key}: {sum(a != b for a, b in zip(want, got)) or "count"} stereo mismatches')
    report('.ses parser stereo vs RTF ground truth', not bad, '; '.join(bad))


SECTIONS = ('inputs', 'aux', 'groups', 'matrix')
DP_SECTIONS = {'inputs': 'Input Channels', 'aux': 'Aux Outputs',
               'groups': 'Group Outputs', 'matrix': 'Matrix Outputs'}


def digipanion_cli(path):
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node or not os.path.exists(path):
        return None
    src = os.path.join(ROOT, 'digipanion-parser', 'digipanion-parse.mjs')
    return json.loads(subprocess.run([node, src, path], capture_output=True,
                                     text=True, check=True).stdout)


def compare_to_digipanion(parsed, dp):
    """[] when every section matches DiGiPanion on names and stereo flags"""
    bad = []
    for key in SECTIONS:
        rows = dp['sections'][DP_SECTIONS[key]]
        got = [(c['name'], c['number'].endswith('s')) for c in parsed[key]]
        want = [(r['name'], r['id'].endswith('s')) for r in rows]
        if got != want:
            n = sum(a != b for a, b in zip(got, want)) or abs(len(got) - len(want))
            bad.append(f'{key}: {n} differences')
    return bad


def test_digipanion_vs_rtf():
    dp = digipanion_cli(SES_FIXTURE)
    if dp is None or not os.path.exists(RTF_FIXTURE):
        report('DiGiPanion vs RTF ground truth (names + stereo)', None, 'node or fixtures missing — skipped')
        return
    app_mod = load_app()
    with open(RTF_FIXTURE, 'rb') as f, contextlib.redirect_stdout(io.StringIO()):
        rtf = app_mod.parse_digico_rtf(f.read().decode('latin-1'))
    rtf = {k: [dict(c, name=c['name'].rstrip()) for c in rtf[k]] for k in SECTIONS}
    bad = compare_to_digipanion(rtf, dp)
    report('DiGiPanion vs RTF ground truth (names + stereo)', not bad, '; '.join(bad))


def test_vo_format(app):
    """Newer console software (session format vO) uses 125-byte channel
    records; the app and Lua parsers must still match DiGiPanion there."""
    dp = digipanion_cli(COPY_AUDIO_FIXTURE)
    if dp is None:
        report('app .ses parser on vO format (raleigh) == DiGiPanion', None, 'node or fixture missing — skipped')
        report('Lua .ses parser on vO format (raleigh) == DiGiPanion', None, 'node or fixture missing — skipped')
        return
    with open(COPY_AUDIO_FIXTURE, 'rb') as f:
        content = f.read()
    with contextlib.redirect_stdout(io.StringIO()):
        parsed = app.parse_ses_show_file(content)
    bad = compare_to_digipanion(parsed, dp)
    report('app .ses parser on vO format (raleigh) == DiGiPanion', not bad, '; '.join(bad))

    try:
        import lupa
    except ImportError:
        report('Lua .ses parser on vO format (raleigh) == DiGiPanion', None, 'lupa not installed — skipped')
        return
    with open(os.path.join(ROOT, 'Lua Scripts', 'DiGiCo_to_Reaper_Full.lua'), encoding='utf-8') as f:
        src = f.read()
    start = src.index('local function parse_ses(content)')
    end = src.index('\n    return out\nend', start) + len('\n    return out\nend')
    parse = lupa.LuaRuntime(encoding=None).execute(src[start:end].encode() + b'\nreturn parse_ses')
    out = parse(content)
    lua_parsed = {}
    for key in SECTIONS:
        t = out[key.encode()]
        lua_parsed[key] = [{'number': t[i][b'number'].decode('latin-1'), 'name': t[i][b'name'].decode('latin-1')}
                           for i in range(1, len(t) + 1)]
    bad = compare_to_digipanion(lua_parsed, dp)
    report('Lua .ses parser on vO format (raleigh) == DiGiPanion', not bad, '; '.join(bad))


def page_record_order_js(app):
    """The page's record-order functions, pulled from the served HTML so the
    test exercises exactly what the browser runs."""
    src = open(os.path.join(ROOT, 'console_to_reaper.py'), encoding='utf-8').read()
    start = src.index('        function copyLabelParts(label) {')
    end = src.index('        function setupRecordOrder(data, session) {')
    return eval("'''" + src[start:end] + "'''")   # same string escaping as serve_html


def test_record_order_alternate_inputs(app):
    """Copy Audio taps a socket. With alternate inputs (A GTR 1/2 -> RC A Gtr's
    socket) the socket's own channel must get the copy, with no shared slots;
    copied preamps that feed no channel still get a track."""
    import shutil
    import subprocess
    node = shutil.which('node')
    ritz = '/Users/michaelleckrone/Documents/00_ritz_foh_start.ses'
    if not node or not os.path.exists(SES_FIXTURE):
        report('record order: alternate inputs (spokane Waves 25-27)', None, 'node or fixture missing — skipped')
        return
    script = page_record_order_js(app) + '''
        const s = JSON.parse(require('fs').readFileSync(0, 'utf8'));
        const ports = copyAudioPorts(s);
        const built = buildRecordOrder({ session: s, port: ports[0].port, consoleInputs: [] });
        process.stdout.write(JSON.stringify({ port: ports[0].port, shared: built.shared,
            tracks: built.tracks.map(t => [t.slot, t.name, t.rec_input]) }));
    '''
    def run(path):
        return json.loads(subprocess.run([node, '-e', script], input=json.dumps(digipanion_cli(path)),
                                         capture_output=True, text=True, check=True).stdout)
    r = run(SES_FIXTURE)
    names = [t[1] for t in r['tracks']]
    ok = (r['port'] == 'Waves' and not r['shared'] and len(names) == 60
          and names[24:27] == ['A GTR 1', 'A GTR 2', 'RC A Gtr']
          and all(t[2] == i for i, t in enumerate(r['tracks'])))
    report('record order: alternate inputs (spokane Waves 25-27)', ok, '' if ok else str(r['shared'] or names[24:27]))
    if os.path.exists(ritz):
        r = run(ritz)
        names = [t[1] for t in r['tracks']]
        ok = r['port'] == 'MADI' and len(names) == 56 and names[48:] == ['1:Mic %d' % n for n in range(49, 57)]
        report('record order: copied preamps without a channel (ritz MADI 49-56)', ok, '' if ok else str(names[46:]))


def test_lua_copy_audio_sync():
    sys.path.insert(0, ROOT)
    import sync_digipanion
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        js = sync_digipanion.build_js(open(sync_digipanion.SOURCE, encoding='utf-8').read())
        helper = sync_digipanion.build_lua_helper(js)
        scripts = {path: open(path, encoding='utf-8').read() for path in sync_digipanion.LUA_SCRIPTS}
    finally:
        os.chdir(cwd)
    stale = []
    for path, lua in scripts.items():
        i = lua.index(sync_digipanion.LUA_OPEN) + len(sync_digipanion.LUA_OPEN) + 1
        if lua[i:lua.index(sync_digipanion.LUA_CLOSE, i)] != helper:
            stale.append(os.path.basename(path))
    report('Lua Copy Audio helper in sync (both scripts)', not stale,
           '' if not stale else ', '.join(stale) + ' — run python3 sync_digipanion.py')
    # MIT: the DiGiPanion copyright notice must ship with every embedded copy
    import digipanion_bundle
    missing = [os.path.basename(p) for p, lua in scripts.items() if 'Copyright (c) 2026 HoboTraveler' not in lua]
    if 'Copyright (c) 2026 HoboTraveler' not in digipanion_bundle.DIGIPANION_JS:
        missing.append('digipanion_bundle.py')
    report('DiGiPanion MIT notice in every embedded copy', not missing, ', '.join(missing))


def run_lua_in_fake_reaper(ses_path, keys, script='DiGiCo_to_Reaper_Full.lua', messages=None):
    """Run a Lua script with stand-in reaper/gfx APIs. keys are
    gfx.getchar() values per frame (13 = Return/import, 111 = O).
    Returns [(name, rec_input)] for the tracks it creates; message boxes
    are appended to messages."""
    import subprocess
    try:
        import lupa.lua54 as lupa
    except ImportError:
        import lupa
    lua = lupa.LuaRuntime(encoding=None)
    g = lua.globals()
    g.py_run = lambda cmd: subprocess.run(cmd.decode(), shell=True, capture_output=True).stdout
    g.SES_PATH = ses_path.encode()
    lua.execute(b'''
        io.popen = function(cmd)   -- Reaper's Lua has popen; route it through Python here
          local out = py_run(cmd)
          return { read = function(self, fmt)
                     if fmt == "*l" then return (out:match("^[^" .. string.char(10) .. "]*")) end
                     return out end,
                   close = function() end }
        end
        tracks, deferred, keyq, msgs = {}, {}, {}, {}
        reaper = {
          GetOS = function() return "OSX64" end,
          GetUserFileNameForRead = function() return true, SES_PATH end,
          ShowMessageBox = function(m) msgs[#msgs+1] = m end,
          defer = function(f) deferred[#deferred+1] = f end,
          Undo_BeginBlock = function() end, Undo_EndBlock = function() end,
          PreventUIRefresh = function() end, TrackList_AdjustWindows = function() end,
          UpdateArrange = function() end,
          CountTracks = function() return #tracks end,
          InsertTrackAtIndex = function() tracks[#tracks+1] = {name="", rec=-1} end,
          GetTrack = function(p, i) return tracks[i+1] end,
          GetSetMediaTrackInfo_String = function(tr, k, v) tr.name = v end,
          SetTrackColor = function() end,
          ColorToNative = function() return 0 end,
          SetMediaTrackInfo_Value = function(tr, k, v) if k == "I_RECINPUT" then tr.rec = v end end,
        }
        gfx = { mouse_x=0, mouse_y=0, mouse_cap=0, mouse_wheel=0, x=0, y=0,
          init=function() end, setfont=function() end, rect=function() end,
          update=function() end, quit=function() end, drawstr=function() end,
          measurestr=function(s) return #s*6, 12 end,
          getchar=function() return table.remove(keyq, 1) or 0 end }
    ''')
    for k in keys:
        lua.execute(b'keyq[#keyq+1] = %d' % k)
    with open(os.path.join(ROOT, 'Lua Scripts', script), 'rb') as f:
        lua.execute(f.read())
    for _ in range(len(keys) + 3):
        pending = lua.eval(b'deferred')
        if len(pending) == 0:
            break
        frame = pending[len(pending)]
        lua.execute(b'deferred = {}')
        frame()
    if messages is not None:
        m = lua.eval(b'msgs')
        messages.extend(m[i].decode('utf-8', 'replace') for i in range(1, len(m) + 1))
    t = lua.eval(b'tracks')
    return [(t[i][b'name'].decode('latin-1'), int(t[i][b'rec'])) for i in range(1, len(t) + 1)]


def test_lua_record_order():
    if sys.platform != 'darwin' or not os.path.exists(SES_FIXTURE):
        report('Lua script record order in stand-in Reaper (spokane)', None, 'needs macOS + fixture — skipped')
        return
    try:
        import lupa  # noqa: F401
    except ImportError:
        report('Lua script record order in stand-in Reaper (spokane)', None, 'lupa not installed — skipped')
        return
    tracks = run_lua_in_fake_reaper(SES_FIXTURE, [0, 13])   # one frame, then Return
    waves = tracks[:60]
    ok = (len(tracks) == 60   # only the Copy Audio tracks are selected by default
          and all(rec == i for i, (_, rec) in enumerate(waves))
          and [n for n, _ in waves[24:27]] == ['A GTR 1', 'A GTR 2', 'RC A Gtr']
          and waves[51][0] == 'Tom 1 Trigger' and waves[59][0] == 'Eli TB')
    report('Lua script record order in stand-in Reaper (spokane)', ok, '' if ok else str(waves[22:28]))

    # One-step script: no window, straight to tracks
    msgs = []
    tracks = run_lua_in_fake_reaper(SES_FIXTURE, [], 'DiGiCo_Copy_Audio_to_Reaper.lua', msgs)
    ok = (len(tracks) == 60 and all(rec == i for i, (_, rec) in enumerate(tracks))
          and [n for n, _ in tracks[24:27]] == ['A GTR 1', 'A GTR 2', 'RC A Gtr']
          and len(msgs) == 1 and msgs[0].startswith('Created 60 tracks for Waves 1-60'))
    report('one-step Copy Audio script (spokane)', ok, '' if ok else str((len(tracks), msgs)))

    # Session with no Copy Audio patch: O explains, tracks import in channel order
    if os.path.exists(NO_COPY_FIXTURE):
        msgs = []
        tracks = run_lua_in_fake_reaper(NO_COPY_FIXTURE, [0, 111, 0, 13], 'DiGiCo_to_Reaper_Full.lua', msgs)
        ok = (tracks[:2] == [('Kick In', 0), ('Kick Out', 1)] and len(msgs) == 1
              and 'no Copy Audio patch' in msgs[0])
        report('Lua script, session without Copy Audio: notice + channel order', ok, '' if ok else str((tracks[:2], msgs)))
        msgs = []
        tracks = run_lua_in_fake_reaper(NO_COPY_FIXTURE, [], 'DiGiCo_Copy_Audio_to_Reaper.lua', msgs)
        ok = not tracks and len(msgs) == 1 and 'no Copy Audio routes' in msgs[0]
        report('one-step script, session without Copy Audio: explains, no tracks', ok, '' if ok else str(msgs))

    msgs = []
    tracks = run_lua_in_fake_reaper(RTF_FIXTURE, [], 'DiGiCo_Copy_Audio_to_Reaper.lua', msgs)
    ok = not tracks and len(msgs) == 1 and '.ses' in msgs[0]
    report('one-step Copy Audio script rejects .rtf', ok, '' if ok else str(msgs))


def test_convert_prints_nothing(app):
    """/convert must not print anything from the uploaded file: on the hosted
    version, server output becomes the host's logs."""
    if not os.path.exists(RTF_FIXTURE):
        report('/convert prints nothing from the file (privacy)', None, 'fixture missing — skipped')
        return
    srv = HTTPServer(('127.0.0.1', 0), app.DiGiCoToReaperHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with open(RTF_FIXTURE, 'rb') as f:
        content = f.read()
    boundary = 'testboundary'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="spokane.rtf"\r\n'
            f'Content-Type: application/rtf\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        req = urllib.request.Request(f'http://127.0.0.1:{srv.server_port}/convert', data=body,
                                     headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    srv.shutdown()
    ok = r.get('success') and r['counts']['inputs'] == 100 and out.getvalue() == ''
    report('/convert prints nothing from the file (privacy)', ok, '' if ok else repr(out.getvalue()[:120]))


def test_digipanion_bundle(app):
    import hashlib
    import shutil
    import subprocess
    src_path = os.path.join(ROOT, 'digipanion-parser', 'digipanion-parse.mjs')
    with open(src_path, encoding='utf-8') as f:
        src = f.read()
    import digipanion_bundle as bundle
    in_sync = bundle.SOURCE_SHA256 == hashlib.sha256(src.encode('utf-8')).hexdigest()
    report('DiGiPanion bundle in sync with parser source', in_sync,
           '' if in_sync else 'run python3 sync_digipanion.py')
    report('app serves the DiGiPanion bundle', app.DIGIPANION_JS == bundle.DIGIPANION_JS)

    node = shutil.which('node')
    if not node or not os.path.exists(COPY_AUDIO_FIXTURE):
        report('DiGiPanion browser bundle == Node CLI (raleigh.ses)', None, 'node or fixture missing — skipped')
        return
    # Run the browser bundle under Node with a stand-in window, compare to the CLI
    script = (
        "const fs = require('fs');"
        "globalThis.window = {};"
        "new Function(fs.readFileSync(0, 'utf8'))();"
        "const f = fs.readFileSync(process.argv[1]);"
        "const s = window.digipanionParseSession(f.buffer.slice(f.byteOffset, f.byteOffset + f.byteLength), 'raleigh.ses');"
        "process.stdout.write(JSON.stringify(s));"
    )
    browser = json.loads(subprocess.run([node, '-e', script, COPY_AUDIO_FIXTURE], input=bundle.DIGIPANION_JS,
                                        capture_output=True, text=True, check=True).stdout)
    cli = json.loads(subprocess.run([node, src_path, COPY_AUDIO_FIXTURE],
                                    capture_output=True, text=True, check=True).stdout)
    report('DiGiPanion browser bundle == Node CLI (raleigh.ses)', browser == cli)

    # Raleigh (most recent show): Waves 1-60, one source each, triggers on 52-55
    waves = {}
    for row in cli['sections']['Input Channels']:
        for label in row['copyAudio']:
            m = re.match(r'(?:\d+:)?Waves (\d+)$', label)
            if m:
                waves.setdefault(int(m.group(1)), []).append(row['name'])
    ok = (sorted(waves) == list(range(1, 61)) and all(len(v) == 1 for v in waves.values())
          and waves[1] == ['Kick In'] and waves[7] == ['Tom 2'] and waves[18] == ['Guest Pack']
          and waves[52] == ['Tom 1 Trigger'] and waves[56] == ['MON TB'])
    report('Raleigh Copy Audio patch (Waves 1-60)', ok)


def test_record_order_template(app):
    # Record order: each track carries its recorder socket (0-based input);
    # a custom track without one goes after the highest fixed input
    channels = [
        {'number': 'CH7', 'name': 'Tom 1 Trigger', 'rec_input': 51},
        {'number': 'CH1', 'name': 'Kick In', 'rec_input': 0},
        {'number': 'Waves 2', 'name': '', 'rec_input': 1, 'is_empty': True},
        {'number': 'CH12s', 'name': 'Verb', 'rec_input': 2},
        {'number': 'C1', 'name': 'Custom'},
    ]
    t = app.generate_reaper_track_template(channels, 'stereo')
    recs = re.findall(r'NAME "([^"]*)"[\s\S]*?REC 1 (\d+) [\s\S]*?NCHAN (\d+)', t)
    ok = recs == [('Tom 1 Trigger', '51', '1'), ('Kick In', '0', '1'), ('', '1', '1'),
                  ('Verb', '2', '1'), ('Custom', '52', '1')]
    report('track template: Copy Audio record order keeps each input', ok, '' if ok else str(recs))


def main():
    print('Console to Reaper — regression tests\n')
    app = load_app()
    test_osc_engine(app)
    test_http_endpoint(app)
    test_embedded_sync()
    test_lua_compiles()
    test_track_template(app)
    test_ses_parser(app)
    test_digipanion_bundle(app)
    test_convert_prints_nothing(app)
    test_digipanion_vs_rtf()
    test_vo_format(app)
    test_record_order_template(app)
    test_record_order_alternate_inputs(app)
    test_lua_copy_audio_sync()
    test_lua_record_order()

    fails = [r for r in RESULTS if r[1] is False]
    skips = [r for r in RESULTS if r[1] is None]
    print(f'\n{len(RESULTS) - len(fails) - len(skips)} passed, '
          f'{len(fails)} failed, {len(skips)} skipped')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
