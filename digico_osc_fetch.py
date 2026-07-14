#!/usr/bin/env python3
"""DiGiCo OSC name + stereo fetcher (iPad command set preferred).

Query-only: every message sent ends in '/?' with no arguments. Bare
addresses and string args are SETTERS on DiGiCo consoles — never send them.

Protocol (verified on Quantum 338 v2242, External Control device type iPad):
    /Console/Channels/?               -> per-section channel counts
    /Console/<Section>/modes/?        -> int array, 1 = mono, 2 = stereo
    /Console/Name/?                   -> console name
    /Console/Session/Filename/?       -> loaded session filename
    /Input_Channels/{n}/Channel_Input/name/?   -> channel name
    /{Aux|Group|Matrix}_Outputs/{n}/Buss_Trim/name/?  -> bus name

Replies arrive without prefix on the iPad command set and with an /sd
prefix on the generic OSC command set; both are accepted (the generic set
answers names only — no counts/modes/console info).

Usage:
    python3 digico_osc_fetch.py <console_ip> [--send-port 8012] [--listen-port 8011]
                                [--tsv] [--json out.json]

Stdlib only.
"""

import argparse
import json
import re
import socket
import struct
import sys
import time


def osc_pad(b):
    return b + b'\x00' * ((4 - len(b) % 4) % 4)


def osc_query(address):
    return osc_pad(address.encode('ascii') + b'\x00') + osc_pad(b',\x00')


def osc_parse(data):
    try:
        if data[:1] != b'/':
            return None
        end = data.index(b'\x00')
        address = data[:end].decode('ascii')
        i = (end + 4) & ~3
        if i >= len(data) or data[i:i+1] != b',':
            return address, []
        tend = data.index(b'\x00', i)
        tags = data[i+1:tend].decode('ascii')
        i = (tend + 4) & ~3
        args = []
        for t in tags:
            if t == 'i':
                args.append(struct.unpack('>i', data[i:i+4])[0]); i += 4
            elif t == 'f':
                args.append(struct.unpack('>f', data[i:i+4])[0]); i += 4
            elif t == 's':
                send = data.index(b'\x00', i)
                args.append(data[i:send].decode('ascii', 'replace'))
                i = (send + 4) & ~3
            else:
                return address, args
        return address, args
    except Exception:
        return None


SECTIONS = [
    # key       osc section        name leaf              default prefix  max
    ('inputs',  'Input_Channels',  'Channel_Input/name',  'Ch',           128),
    ('aux',     'Aux_Outputs',     'Buss_Trim/name',      'Aux',          48),
    ('groups',  'Group_Outputs',   'Buss_Trim/name',      'Grp',          24),
    ('matrix',  'Matrix_Outputs',  'Buss_Trim/name',      'Matrix',       24),
]


def fetch(console_ip, send_port, listen_port, quiet=False):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', listen_port))
    sock.settimeout(0.05)
    dest = (console_ip, send_port)

    def log(msg):
        if not quiet:
            print(msg, file=sys.stderr)

    def collect(max_wait, sink, done=None, idle=0.25):
        """Read replies until done() is satisfied, the line goes idle after
        at least one reply, or max_wait elapses."""
        deadline = time.time() + max_wait
        last_rx = None
        while time.time() < deadline:
            if done and done():
                break
            if last_rx is not None and (time.time() - last_rx) > idle:
                break
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            p = osc_parse(data)
            if p:
                sink(p[0], p[1])
                last_rx = time.time()

    # ---- phase 1: console info, counts, stereo modes (iPad set only) ----
    info = {'counts': {}, 'modes': {}, 'console': '', 'session': '',
            'alive': False}

    def sink1(addr, args):
        info['alive'] = True
        if addr == '/Console/Name' and args:
            info['console'] = str(args[0])
        elif addr == '/Console/Session/Filename' and args:
            info['session'] = str(args[0])
        else:
            m = re.match(r'^/Console/(\w+)/modes$', addr)
            if m:
                info['modes'][m.group(1)] = [int(v) for v in args]
                return
            m = re.match(r'^/Console/(\w+)$', addr)
            if m and args:
                try:
                    info['counts'][m.group(1)] = int(args[0])
                except (TypeError, ValueError):
                    pass

    # the input-1 name query doubles as a reachability probe: the generic
    # command set ignores /Console queries but answers names
    for q in ['/Console/Name/?', '/Console/Session/Filename/?', '/Console/Channels/?',
              '/Console/Input_Channels/modes/?', '/Console/Aux_Outputs/modes/?',
              '/Console/Group_Outputs/modes/?', '/Console/Matrix_Outputs/modes/?',
              '/Input_Channels/1/Channel_Input/name/?']:
        sock.sendto(osc_query(q), dest)
        time.sleep(0.01)
    # matrix modes never answers, so "everything" = counts + 3 mode arrays
    collect(1.2, sink1, done=lambda: (
        len(info['counts']) >= 4 and len(info['modes']) >= 3
        and info['console'] and info['session']))
    mode_lens = {k: len(v) for k, v in info['modes'].items()}
    log(f"console={info['console']!r} session={info['session']!r} "
        f"counts={info['counts']} modes={mode_lens}")

    # nothing at all answered: console unreachable — fail fast instead of
    # grinding through the retry rounds
    if not info['alive']:
        log('no response — console unreachable')
        sock.close()
        return {'_console': '', '_session': '',
                'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    # ---- phase 2: pipelined name queries ----
    pending = {}   # acceptable reply address (no prefix) -> (key, n)
    queries = {}
    for key, section, leaf, _, max_n in SECTIONS:
        count = info['counts'].get(section, max_n)
        for n in range(1, min(count, max_n) + 1):
            want = f'/{section}/{n}/{leaf}'
            pending[want] = (key, n)
            queries[want] = osc_query(want + '/?')

    names = {}

    def sink2(addr, args):
        a = addr[3:] if addr.startswith('/sd/') else addr  # accept /sd prefix
        if a in pending and args and isinstance(args[0], str):
            names[pending.pop(a)] = args[0]

    for attempt in range(3):
        if not pending:
            break
        got_before = len(names)
        for want, pkt in queries.items():
            if want in pending:
                sock.sendto(pkt, dest)
                time.sleep(0.002)
        # retries answer within milliseconds; only the first pass needs
        # a generous window
        collect(1.2 if attempt == 0 else 0.5, sink2,
                done=lambda: not pending, idle=0.35)
        log(f'name pass {attempt + 1}: {len(names)} replies, {len(pending)} outstanding')
        if attempt == 0 and pending:
            # channels above the highest reply per section don't exist
            # (only matters when counts were unavailable); keep a margin
            # for genuinely dropped replies near the top
            top = {}
            for (k, n) in names:
                top[k] = max(top.get(k, 0), n)
            for want, (k, n) in list(pending.items()):
                if n > top.get(k, 0) + 8:
                    del pending[want]
        if attempt > 0 and len(names) == got_before:
            break  # nothing new is coming
    sock.close()

    # ---- assemble ----
    result = {'_console': info['console'], '_session': info['session']}
    for key, section, leaf, def_prefix, _ in SECTIONS:
        def_re = re.compile(r'^%s \d+$' % def_prefix)
        modes = info['modes'].get(section, [])
        got_ns = [n for (k, n) in names if k == key]
        count = max(got_ns) if got_ns else 0
        out = []
        for n in range(1, count + 1):
            name = names.get((key, n))
            if name is None:
                continue
            name = name.rstrip()
            is_default = (not name) or bool(def_re.match(name))
            stereo = (modes[n-1] == 2) if n <= len(modes) else False
            out.append({'number': n, 'name': name or f'{def_prefix} {n}',
                        'is_default': is_default, 'stereo': stereo})
        result[key] = out
        named = sum(1 for c in out if not c['is_default'])
        st = sum(1 for c in out if c['stereo'])
        log(f'{key}: {len(out)} channels, {named} named, {st} stereo')
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('console_ip')
    ap.add_argument('--send-port', type=int, default=8012)
    ap.add_argument('--listen-port', type=int, default=8011)
    ap.add_argument('--json', metavar='FILE', help='write JSON here (- for stdout)')
    ap.add_argument('--tsv', action='store_true',
                    help='machine-readable: section<TAB>num<TAB>name<TAB>is_default<TAB>stereo')
    args = ap.parse_args()

    t0 = time.time()
    try:
        result = fetch(args.console_ip, args.send_port, args.listen_port,
                       quiet=bool(args.json or args.tsv))
    except OSError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)

    if args.tsv:
        print(f"meta\t0\t{result.get('_console', '')}\t0\t0")
        print(f"meta\t1\t{result.get('_session', '')}\t0\t0")
        for key, _, _, _, _ in SECTIONS:
            for c in result.get(key, []):
                print(f'{key}\t{c["number"]}\t{c["name"]}'
                      f'\t{1 if c["is_default"] else 0}\t{1 if c["stereo"] else 0}')
    elif args.json:
        payload = json.dumps(result)
        if args.json == '-':
            print(payload)
        else:
            with open(args.json, 'w') as f:
                f.write(payload)
    else:
        print(f"\nFetched in {time.time() - t0:.1f}s from "
              f"{result.get('_console') or args.console_ip}"
              f" (session: {result.get('_session') or 'unknown'})\n")
        for key, _, _, _, _ in SECTIONS:
            chans = result.get(key, [])
            named = [c for c in chans if not c['is_default']]
            print(f'--- {key.upper()} ({len(chans)} channels, {len(named)} named) ---')
            for c in chans:
                s = 's' if c['stereo'] else ' '
                flag = '   (default)' if c['is_default'] else ''
                print(f'  {c["number"]:>3}{s}: {c["name"]}{flag}')
            print()


if __name__ == '__main__':
    main()
