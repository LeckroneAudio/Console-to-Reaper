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

# fixture locations (skipped gracefully when absent)
SES_FIXTURE = '/Users/michaelleckrone/Desktop/Console-to-Reaper Test Files/DiGiCo/spokane.ses'
RTF_FIXTURE = '/Users/michaelleckrone/Desktop/Console-to-Reaper Test Files/DiGiCo/spokane.rtf'

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


def main():
    print('Console to Reaper — regression tests\n')
    app = load_app()
    test_osc_engine(app)
    test_http_endpoint(app)
    test_embedded_sync()
    test_lua_compiles()
    test_track_template(app)
    test_ses_parser(app)

    fails = [r for r in RESULTS if r[1] is False]
    skips = [r for r in RESULTS if r[1] is None]
    print(f'\n{len(RESULTS) - len(fails) - len(skips)} passed, '
          f'{len(fails)} failed, {len(skips)} skipped')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
