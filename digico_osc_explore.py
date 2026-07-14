#!/usr/bin/env python3
"""DiGiCo OSC stereo-flag hunt — structured experiments, digest output.

All traffic is query-only ('/?' suffix, no arguments). Safe.

Experiments:
  A. leaf existence battery on known-stereo channels (an existing leaf
     answers with ONE message; a nonexistent one triggers a full channel
     dump of ~230 messages)
  B. subtree container queries (may reveal leaves missing from full dumps)
  C. global/console-level roots
  D. dump diffs: stereo vs mono group and aux — different ADDRESS SETS
     would be a stereo signature

Usage:
    python3 digico_osc_explore.py <console_ip> [--send-port 8012] [--listen-port 8011]
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


INTERESTING = ('stereo', 'format', 'width', 'pair', 'link', 'mono',
               'balance', 'image', 'type', 'config')


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
        """Drain replies for `wait` seconds -> {address: last args}."""
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

    def query(addr, wait=1.2):
        sock.sendto(osc_query(addr + '/?'), dest)
        return collect(wait)

    collect(0.3)  # drain any stale traffic

    # ---- A: leaf existence battery ----
    print('=== A: leaf existence tests (1 msg = leaf EXISTS, flood = not) ===')
    batteries = [
        ('Input_Channels/84', ['Channel_Input/stereo', 'Channel_Input/format',
                               'Channel_Input/width', 'Channel_Input/mono',
                               'stereo', 'format', 'width', 'mono', 'pair',
                               'Panner/width', 'Panner/balance']),
        ('Group_Outputs/7',   ['Buss_Trim/stereo', 'Buss_Trim/format',
                               'stereo', 'format', 'width', 'mono',
                               'balance', 'pan']),
        ('Aux_Outputs/1',     ['Buss_Trim/stereo', 'Buss_Trim/format',
                               'stereo', 'format', 'width', 'mono',
                               'balance', 'pan']),
    ]
    for base, leaves in batteries:
        for leaf in leaves:
            addr = f'/{base}/{leaf}'
            got = query(addr)
            expected = f'/sd/{base}/{leaf}'
            if expected in got and len(got) <= 3:
                print(f'  EXISTS  {addr}  ->  {got[expected]}')
            elif len(got) == 0:
                print(f'  silent  {addr}')
            else:
                print(f'  flood({len(got)})  {addr}')

    # ---- B: container subtree queries ----
    print('\n=== B: container queries (unique addresses returned) ===')
    for base in ['Input_Channels/84/Channel_Input', 'Input_Channels/84/Panner',
                 'Group_Outputs/7/Buss_Trim', 'Aux_Outputs/1/Buss_Trim']:
        got = query(f'/{base}', wait=1.5)
        keys = sorted(got)
        print(f'  /{base}/?  ->  {len(keys)} addresses')
        for k in keys[:12]:
            print(f'      {k}  {got[k]}')

    # ---- C: global roots ----
    print('\n=== C: global roots ===')
    for root in ['Console', 'Console/Name', 'Console/Info', 'Session',
                 'Layout', 'Channel_List', 'Console/Channels']:
        got = query(f'/{root}', wait=1.5)
        print(f'  /{root}/?  ->  {len(got)} addresses')
        for k in sorted(got)[:10]:
            print(f'      {k}  {got[k]}')

    # ---- D: dump diffs, stereo vs mono ----
    print('\n=== D: dump diffs (address-set differences) ===')
    pairs = [
        ('Group_Outputs/7', 'Group_Outputs/4', 'grp7=Master(stereo?) vs grp4=default(mono?)'),
        ('Aux_Outputs/1',   'Aux_Outputs/21',  'aux1=EVAN(stereo?) vs aux21=ASL(mono?)'),
    ]
    import re as _re
    for a, b, label in pairs:
        da = query(f'/{a}/zzz_nonexistent', wait=2.5)
        db = query(f'/{b}/zzz_nonexistent', wait=2.5)
        # normalize the channel number segment so address sets are comparable
        na = {_re.sub(r'/\d+/', '/N/', k, count=1): v for k, v in da.items()}
        nb = {_re.sub(r'/\d+/', '/N/', k, count=1): v for k, v in db.items()}
        only_a = sorted(set(na) - set(nb))
        only_b = sorted(set(nb) - set(na))
        print(f'  {label}: {len(da)} vs {len(db)} leaves')
        for k in only_a[:15]:
            print(f'      only in first : {k}  {na[k]}')
        for k in only_b[:15]:
            print(f'      only in second: {k}  {nb[k]}')
        if not only_a and not only_b:
            print('      (identical address sets)')

    # highlight anything interesting seen anywhere
    print('\n=== interesting substrings seen in any reply address ===')
    # (rerun a broad drain in case console pushed anything)
    sock.close()
    print('Done.')


if __name__ == '__main__':
    main()
