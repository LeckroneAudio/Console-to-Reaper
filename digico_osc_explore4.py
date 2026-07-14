#!/usr/bin/env python3
"""DiGiCo OSC round 4 — iPad set: /Console/Channels contents + bus stereo hunt.

The iPad command set exposes more leaves per bus (aux 167 vs 132, group 311
vs 143 under the generic set). This round prints the full /Console/Channels
answer (9 messages) and diffs full stereo-vs-mono bus dumps to find the bus
stereo flag.

All traffic is query-only. Safe.

Usage:
    python3 digico_osc_explore4.py <console_ip> [--send-port 8012] [--listen-port 8011]
"""

import argparse
import re
import socket
import struct
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
                args.append(round(struct.unpack('>f', data[i:i+4])[0], 4)); i += 4
            elif t == 's':
                send = data.index(b'\x00', i)
                args.append(data[i:send].decode('ascii', 'replace'))
                i = (send + 4) & ~3
            else:
                return address, args
        return address, args
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('console_ip')
    ap.add_argument('--send-port', type=int, default=8012)
    ap.add_argument('--listen-port', type=int, default=8011)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', args.listen_port))
    sock.settimeout(0.05)
    dest = (args.console_ip, args.send_port)

    def collect(wait):
        got = {}
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            p = osc_parse(data)
            if p:
                got[p[0]] = p[1]
        return got

    def query(addr, wait):
        collect(0.3)
        sock.sendto(osc_query(addr + '/?'), dest)
        return collect(wait)

    # ---- 1: full /Console/Channels answer ----
    print('=== /Console/Channels/? — full contents ===')
    got = query('/Console/Channels', 2.0)
    for k in sorted(got):
        print(f'  {k}  {got[k]}')

    # ---- 2: bus dumps under iPad set, stereo vs mono ----
    KEYWORDS = ('stereo', 'mode', 'width', 'mono', 'pair', 'link', 'format', 'balance')

    def dump(base):
        collect(0.5)
        sock.sendto(osc_query(f'/{base}/zzz_nope/?'), dest)
        return collect(4.0)

    pairs = [
        ('Aux_Outputs/1', 'Aux_Outputs/21', 'AUX 1 stereo vs AUX 21 mono'),
        ('Group_Outputs/7', 'Group_Outputs/4', 'GRP 7 stereo vs GRP 4 mono'),
    ]
    for a, b, label in pairs:
        da = dump(a)
        time.sleep(2.0)
        db = dump(b)
        time.sleep(2.0)
        na = {re.sub(r'/\d+/', '/N/', k, count=1): v for k, v in da.items()}
        nb = {re.sub(r'/\d+/', '/N/', k, count=1): v for k, v in db.items()}
        print(f'\n=== {label}: {len(da)} vs {len(db)} leaves ===')
        only_a = sorted(set(na) - set(nb))
        only_b = sorted(set(nb) - set(na))
        for k in only_a:
            print(f'  only stereo side: {k}  {na[k]}')
        for k in only_b:
            print(f'  only mono side  : {k}  {nb[k]}')
        # leaves matching stereo-ish keywords, with values side by side
        print('  keyword leaves:')
        for k in sorted(set(na) | set(nb)):
            if any(w in k.lower() for w in KEYWORDS):
                print(f'    {k:55s} stereo={na.get(k)}  mono={nb.get(k)}')
        # value differences on shared leaves (candidates if no address diff)
        diffs = [(k, na[k], nb[k]) for k in sorted(set(na) & set(nb)) if na[k] != nb[k]]
        print(f'  {len(diffs)} shared leaves differ in value (name/levels expected); '
              f'non-audio-looking ones:')
        for k, va, vb in diffs:
            leaf = k.split('/')[-1]
            if not any(w in leaf for w in ('gain', 'thresh', 'freq', 'name', 'level',
                                           'delay', 'attack', 'release', 'ratio', 'knee',
                                           'trim', 'Q_', 'crossover', 'pan', 'fader')):
                print(f'    {k:55s} stereo={va}  mono={vb}')

    sock.close()
    print('\nDone.')


if __name__ == '__main__':
    main()
