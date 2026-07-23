#!/usr/bin/env python3
"""Sync the fetcher embedded in DiGiCo_OSC_to_Reaper.lua from the canonical
digico_osc_fetch.py.

The Lua script carries a verbatim copy of digico_osc_fetch.py inside a
[==[ ... ]==] block (ReaScript has no UDP, so it writes the copy to a temp
file and runs it with --tsv). Run this after any change to
digico_osc_fetch.py so the Reaper script's offline fallback never drifts
from the app's engine.

Usage:  python3 sync_embedded_fetcher.py
"""

import re
import sys

FETCHER = 'digico_osc_fetch.py'
LUA = 'Lua Scripts/DiGiCo_OSC_to_Reaper.lua'


def main():
    src = open(FETCHER).read()
    if ']==]' in src:
        sys.exit(f'ERROR: {FETCHER} contains "]==]" which would break the '
                 'Lua long-string block. Rewrite that sequence first.')

    lua = open(LUA).read()
    m = re.search(r'(local PYFETCH = \[==\[\n).*?(\]==\])', lua, re.S)
    if not m:
        sys.exit(f'ERROR: PYFETCH block not found in {LUA}')

    new_lua = lua[:m.start()] + m.group(1) + src + m.group(2) + lua[m.end():]
    if new_lua == lua:
        print('already in sync')
        return
    open(LUA, 'w').write(new_lua)
    print(f'synced {len(src)} bytes from {FETCHER} into {LUA}')


if __name__ == '__main__':
    main()
