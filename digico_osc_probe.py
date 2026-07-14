#!/usr/bin/env python3
"""DiGiCo OSC probe — discover the console's name-query protocol.

Sends candidate OSC queries to a DiGiCo console (or offline editor) and
prints every reply, so we can learn the exact address patterns for
reading channel names.

Usage:
    python3 digico_osc_probe.py <console_ip> [--send-port 8000] [--listen-port 9000]
    python3 digico_osc_probe.py <console_ip> --listen-only     # just sniff replies

Console setup (Setup > External Control):
    Enable External Control: YES
    Add device: type "iPad" (or "Other OSC"), IP = this computer,
    Send port = --listen-port value, Receive port = --send-port value.

Stdlib only — no dependencies.
"""

import argparse
import socket
import struct
import sys
import threading
import time


# ---------------------------------------------------------------- OSC codec

def osc_pad(b):
    return b + b'\x00' * ((4 - len(b) % 4) % 4)


def osc_message(address, *args):
    """Encode an OSC message. Args may be int, float, or str."""
    msg = osc_pad(address.encode('ascii') + b'\x00')
    tags = ','
    payload = b''
    for a in args:
        if isinstance(a, float):
            tags += 'f'
            payload += struct.pack('>f', a)
        elif isinstance(a, int):
            tags += 'i'
            payload += struct.pack('>i', a)
        else:
            tags += 's'
            payload += osc_pad(str(a).encode('ascii') + b'\x00')
    msg += osc_pad(tags.encode('ascii') + b'\x00')
    msg += payload
    return msg


def osc_parse(data):
    """Decode one OSC message -> (address, [args]). Returns None if invalid."""
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
            elif t == 'b':
                blen = struct.unpack('>i', data[i:i+4])[0]
                args.append(f'<blob {blen}B>')
                i = (i + 4 + blen + 3) & ~3
            else:
                args.append(f'<{t}?>')
        return address, args
    except Exception:
        return None


# ---------------------------------------------------------------- probe

def listener(sock, stop):
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        parsed = osc_parse(data)
        ts = time.strftime('%H:%M:%S')
        if parsed:
            print(f'  [{ts}] RECV from {addr[0]}:{addr[1]}  {parsed[0]}  {parsed[1]}')
        else:
            print(f'  [{ts}] RECV from {addr[0]}:{addr[1]}  (unparsed) {data[:80]!r}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('console_ip')
    ap.add_argument('--send-port', type=int, default=8000,
                    help='port the console receives on (default 8000)')
    ap.add_argument('--listen-port', type=int, default=9000,
                    help='port the console sends replies to (default 9000)')
    ap.add_argument('--listen-only', action='store_true',
                    help='send nothing; just print incoming OSC')
    ap.add_argument('--channels', type=int, default=2,
                    help='how many channels to query per section (default 2)')
    ap.add_argument('--dump', metavar='SECTION/N',
                    help='dump full state of one channel, e.g. Input_Channels/84 '
                         'or Aux_Outputs/3 (query-only, safe)')
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    try:
        sock.bind(('0.0.0.0', args.listen_port))
    except OSError as e:
        print(f'Cannot bind listen port {args.listen_port}: {e}')
        print('(Is Companion or your bridge already using it? Pick another '
              'with --listen-port and update the console device entry.)')
        sys.exit(1)

    stop = threading.Event()
    t = threading.Thread(target=listener, args=(sock, stop), daemon=True)
    t.start()

    print(f'Listening on UDP {args.listen_port}; console at '
          f'{args.console_ip}:{args.send_port}')

    if args.listen_only:
        print('Listen-only mode. Ctrl-C to quit. Try renaming a channel on '
              'the console and watch for traffic.')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        stop.set()
        return

    def send(addr, *a):
        pkt = osc_message(addr, *a)
        sock.sendto(pkt, (args.console_ip, args.send_port))
        arg_txt = f'  {list(a)}' if a else ''
        print(f'SEND {addr}{arg_txt}')
        time.sleep(0.25)

    if args.dump:
        # Full channel state via query on the channel root and on a
        # nonexistent leaf (both trigger complete dumps on some firmwares).
        base = '/' + args.dump.strip('/')
        for q in (base + '/?', base + '/zzz_nonexistent/?'):
            send(q)
            time.sleep(2)
        print('\nWaiting 5s for stragglers...')
        time.sleep(5)
        stop.set()
        return

    # ---- global / console-level queries ----
    print('\n--- console-level queries ---')
    for addr in [
        '/Console/Name/?',
        '/sd/Console/Name/?',
        '/Console/Channels/?',
        '/sd/Console/Channels/?',
        '/Console/Session/Filename/?',
    ]:
        send(addr)

    # ---- per-channel name queries, both path roots and query styles ----
    sections = [
        ('Input_Channels',  'Channel_Input/name'),
        ('Aux_Outputs',     'Channel_Input/name'),
        ('Group_Outputs',   'Channel_Input/name'),
        ('Matrix_Outputs',  'Channel_Input/name'),
        ('Aux_Outputs',     'Buss_Trim/name'),
        ('Group_Outputs',   'Buss_Trim/name'),
    ]
    # WARNING (learned the hard way): bare addresses and string args are
    # SETTERS on DiGiCo consoles — a query MUST end in '/?' with no args.
    print('\n--- channel name queries ---')
    for n in range(1, args.channels + 1):
        for sec, leaf in sections:
            for root in ('', '/sd'):
                send(f'{root}/{sec}/{n}/{leaf}/?')

    print('\nWaiting 5s for stragglers...')
    time.sleep(5)
    stop.set()
    print('Done. Anything printed as RECV above is the protocol talking back.')


if __name__ == '__main__':
    main()
