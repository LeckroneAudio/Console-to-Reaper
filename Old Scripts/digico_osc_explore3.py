#!/usr/bin/env python3
"""DiGiCo OSC round 3 — iPad command set: query names + stereo_mode.

The iPad command set pushes /Input_Channels/{n}/Channel_Input/stereo_mode
(1.0 = mono, 2.0 = stereo) on changes. This round verifies we can QUERY it
on demand, finds the equivalent path for aux/group/matrix busses, and
confirms name queries still work under this command set.

All traffic is query-only ('/?' suffix, no args). Safe.

Usage:
    python3 digico_osc_explore3.py <console_ip> [--send-port 8012] [--listen-port 8011]
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

    def test(addr, wait=1.2):
        collect(0.3)
        sock.sendto(osc_query(addr + '/?'), dest)
        got = collect(wait)
        if len(got) == 0:
            verdict = 'silent'
        elif len(got) <= 3:
            verdict = '  '.join(f'{k} {v}' for k, v in got.items())
        else:
            verdict = f'flood({len(got)})'
        print(f'  {addr:52s} -> {verdict}')
        time.sleep(0.5)
        return got

    print('=== name queries under iPad command set ===')
    test('/Input_Channels/1/Channel_Input/name')
    test('/Aux_Outputs/1/Buss_Trim/name')
    test('/Group_Outputs/7/Buss_Trim/name')
    test('/Matrix_Outputs/1/Buss_Trim/name')

    print('\n=== stereo_mode queries: inputs (1,2 mono; 84,91 stereo) ===')
    for n in [1, 2, 84, 91]:
        test(f'/Input_Channels/{n}/Channel_Input/stereo_mode')

    print('\n=== stereo_mode path hunt: aux (1 stereo, 21 mono) ===')
    for path in ['Buss_Trim/stereo_mode', 'stereo_mode', 'Channel_Input/stereo_mode']:
        test(f'/Aux_Outputs/1/{path}')
        test(f'/Aux_Outputs/21/{path}')

    print('\n=== stereo_mode path hunt: groups (7 stereo, 4 mono) ===')
    for path in ['Buss_Trim/stereo_mode', 'stereo_mode']:
        test(f'/Group_Outputs/7/{path}')
        test(f'/Group_Outputs/4/{path}')

    print('\n=== stereo_mode path hunt: matrix (1 mono) ===')
    for path in ['Buss_Trim/stereo_mode', 'stereo_mode']:
        test(f'/Matrix_Outputs/1/{path}')

    print('\n=== console-level queries (retry under iPad set) ===')
    for addr in ['/Console/Name', '/Console/Channels', '/Console/Session/Filename']:
        test(addr)

    sock.close()
    print('\nDone.')


if __name__ == '__main__':
    main()
