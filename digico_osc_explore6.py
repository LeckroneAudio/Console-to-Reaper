#!/usr/bin/env python3
"""DiGiCo OSC round 6 — /Console subtree: per-bus structure (width?).

/Console/Channels answered with section counts — structure lives here.
Bus width IS structure. Probe the tree for per-bus nodes.

All traffic is query-only. Safe.

Usage:
    python3 digico_osc_explore6.py <console_ip> [--send-port 8012] [--listen-port 8011]
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

    def test(addr, wait=1.5):
        collect(0.3)
        sock.sendto(osc_query(addr + '/?'), dest)
        got = collect(wait)
        if len(got) == 0:
            print(f'  {addr:46s} -> silent')
        elif len(got) <= 12:
            print(f'  {addr:46s} -> {len(got)} replies:')
            for k in sorted(got):
                print(f'      {k}  {got[k]}')
        else:
            print(f'  {addr:46s} -> flood({len(got)})')
        time.sleep(0.6)
        return got

    print('=== /Console per-bus structure hunt ===')
    test('/Console/Aux_Outputs')
    test('/Console/Aux_Outputs/1')
    test('/Console/Aux_Outputs/1/stereo')
    test('/Console/Aux_Outputs/1/width')
    test('/Console/Aux_Outputs/1/stereo_mode')
    test('/Console/Group_Outputs/7')
    test('/Console/Group_Outputs/7/stereo_mode')
    test('/Console/Input_Channels/1')
    test('/Console/Session')
    test('/Console/Info')
    test('/Console/Structure')

    sock.close()
    print('\nDone.')


if __name__ == '__main__':
    main()
