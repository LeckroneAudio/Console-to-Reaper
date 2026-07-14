#!/usr/bin/env python3
"""DiGiCo OSC round 5 — iPad set: bus stereo via dumps + send_pan map.

Dump trigger under the iPad command set: querying a REAL leaf name on the
channel root (e.g. /Group_Outputs/7/stereo_mode/?) floods the full channel
state; garbage names are silently ignored.

Goals:
  1. Full iPad-set dumps of stereo vs mono aux and group — diff for a
     width/stereo leaf (iPad dumps have more leaves than generic ones)
  2. One input-channel dump — the set of Aux_Send/{k}/send_pan leaves
     enumerates every stereo aux in one shot
  3. Matrix dump for reference

All traffic is query-only. Safe.

Usage:
    python3 digico_osc_explore5.py <console_ip> [--send-port 8012] [--listen-port 8011]
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


KEYWORDS = ('stereo', 'mode', 'width', 'mono', 'pair', 'link', 'format',
            'balance', 'image')


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

    def dump(base):
        """Full channel dump: query a real leaf name on the channel root."""
        collect(0.5)
        sock.sendto(osc_query(f'/{base}/stereo_mode/?'), dest)
        got = collect(4.0)
        print(f'  dumped /{base}: {len(got)} leaves')
        time.sleep(2.0)
        return got

    def norm(d):
        return {re.sub(r'/\d+/', '/N/', k, count=1): v for k, v in d.items()}

    print('=== dumps ===')
    d_aux_s = dump('Aux_Outputs/1')     # stereo (EVAN)
    d_aux_m = dump('Aux_Outputs/22')    # mono (ASL, shifted by the insert)
    d_grp_s = dump('Group_Outputs/7')   # stereo (Master)
    d_grp_m = dump('Group_Outputs/4')   # mono
    d_in1   = dump('Input_Channels/1')  # for the send_pan map
    d_mtx   = dump('Matrix_Outputs/1')  # reference

    for da, db, label in [(d_aux_s, d_aux_m, 'AUX stereo vs mono'),
                          (d_grp_s, d_grp_m, 'GROUP stereo vs mono')]:
        na, nb = norm(da), norm(db)
        print(f'\n=== {label}: {len(da)} vs {len(db)} leaves ===')
        for k in sorted(set(na) - set(nb)):
            print(f'  only stereo: {k}  {na[k]}')
        for k in sorted(set(nb) - set(na)):
            print(f'  only mono:   {k}  {nb[k]}')
        print('  keyword leaves:')
        for k in sorted(set(na) | set(nb)):
            if any(w in k.lower() for w in KEYWORDS):
                print(f'    {k:55s} stereo={na.get(k)}  mono={nb.get(k)}')

    print('\n=== stereo aux map from Input_Channels/1 dump (send_pan set) ===')
    pans = sorted({int(m.group(1)) for k in d_in1
                   if (m := re.search(r'Aux_Send/(\d+)/send_pan', k))})
    ons = sorted({int(m.group(1)) for k in d_in1
                  if (m := re.search(r'Aux_Send/(\d+)/send_on', k))})
    print(f'  auxes with send_pan (stereo): {pans}')
    print(f'  auxes with send_on  (all)   : {ons}')

    print('\n=== group/matrix send leaves on input 1 (stereo hints?) ===')
    for k in sorted(norm(d_in1)):
        if 'Group_Send' in k or 'Matrix' in k:
            print(f'  {k}  {norm(d_in1)[k]}')

    print('\n=== input 1 keyword leaves ===')
    for k in sorted(norm(d_in1)):
        if any(w in k.lower() for w in KEYWORDS):
            print(f'  {k}  {norm(d_in1)[k]}')

    print('\n=== matrix 1 keyword leaves ===')
    for k in sorted(norm(d_mtx)):
        if any(w in k.lower() for w in KEYWORDS):
            print(f'  {k}  {norm(d_mtx)[k]}')

    sock.close()
    print('\nDone.')


if __name__ == '__main__':
    main()
