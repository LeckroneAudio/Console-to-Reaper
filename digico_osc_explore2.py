#!/usr/bin/env python3
"""DiGiCo OSC stereo hunt, round 2 — the 'width' asymmetry test.

Round 1 showed: nonexistent leaves trigger a ~230-message flood, but
'width' went SILENT on the three presumed-stereo channels. Hypothesis:
'width' is a real parameter that exists only on stereo channels (silent =
recognized; flood = unknown leaf). This round tests width against known
MONO channels too, with pacing so the console's anti-flood doesn't skew
results, and re-runs the stereo-vs-mono dump diff cleanly.

All traffic is query-only ('/?' suffix). Safe.

Usage:
    python3 digico_osc_explore2.py <console_ip> [--send-port 8012] [--listen-port 8011]
"""

import argparse
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
        collect(0.3)  # drain leftovers first
        sock.sendto(osc_query(addr + '/?'), dest)
        return collect(wait)

    # ---- width asymmetry test across known mono AND stereo channels ----
    # expected (current session): in1 mono, in84 stereo; grp4 mono,
    # grp7 stereo; aux21 mono, aux1 stereo; mtx1 mono
    tests = [
        ('Input_Channels/1',   'mono?'),
        ('Input_Channels/84',  'stereo?'),
        ('Input_Channels/2',   'mono?'),
        ('Input_Channels/91',  'stereo?'),
        ('Group_Outputs/4',    'mono?'),
        ('Group_Outputs/7',    'stereo?'),
        ('Aux_Outputs/21',     'mono?'),
        ('Aux_Outputs/1',      'stereo?'),
        ('Matrix_Outputs/1',   'mono?'),
    ]
    print('=== width query per channel (silent vs flood vs value) ===')
    for base, guess in tests:
        got = query(f'/{base}/width', wait=1.5)
        want = f'/sd/{base}/width'
        if want in got:
            verdict = f'REPLY {got[want]}'
        elif len(got) == 0:
            verdict = 'silent'
        else:
            verdict = f'flood({len(got)})'
        print(f'  {base:22s} [{guess:7s}] -> {verdict}')
        time.sleep(1.0)  # give the console breathing room

    # ---- extra width-adjacent candidates on one stereo channel ----
    print('\n=== extra candidates on Input_Channels/84 ===')
    for leaf in ['Width', 'stereo_width', 'image', 'balance']:
        got = query(f'/Input_Channels/84/{leaf}', wait=1.5)
        want = f'/sd/Input_Channels/84/{leaf}'
        if want in got:
            v = f'REPLY {got[want]}'
        elif len(got) == 0:
            v = 'silent'
        else:
            v = f'flood({len(got)})'
        print(f'  {leaf:14s} -> {v}')
        time.sleep(1.0)

    # ---- clean dump diff: stereo vs mono (paced, long waits) ----
    print('\n=== dump diff, values included (paced) ===')
    import re as _re

    def dump(base):
        collect(0.5)
        sock.sendto(osc_query(f'/{base}/zzz_nope/?'), dest)
        return collect(4.0)

    for a, b, label in [('Group_Outputs/7', 'Group_Outputs/4', 'grp7 stereo vs grp4 mono'),
                        ('Aux_Outputs/1', 'Aux_Outputs/21', 'aux1 stereo vs aux21 mono')]:
        da = dump(a)
        time.sleep(2.0)
        db = dump(b)
        time.sleep(2.0)
        na = {_re.sub(r'/\d+/', '/N/', k, count=1): v for k, v in da.items()}
        nb = {_re.sub(r'/\d+/', '/N/', k, count=1): v for k, v in db.items()}
        print(f'  {label}: {len(da)} vs {len(db)} leaves')
        only_a = sorted(set(na) - set(nb))
        only_b = sorted(set(nb) - set(na))
        for k in only_a:
            print(f'    only stereo side: {k}  {na[k]}')
        for k in only_b:
            print(f'    only mono side  : {k}  {nb[k]}')
        if not only_a and not only_b and da:
            print('    (identical address sets)')

    sock.close()
    print('\nDone.')


if __name__ == '__main__':
    main()
