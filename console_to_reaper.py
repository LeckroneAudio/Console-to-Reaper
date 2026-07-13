#!/usr/bin/env python3
"""
Console to Reaper Track Template Converter
Parses show files from multiple consoles and generates Reaper track templates
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import io
import json
import os
import re
import struct
import subprocess
import tarfile
import uuid
import socket
import threading
import zlib
from urllib.parse import parse_qs

try:
    import rumps
except ImportError:
    print("Installing rumps...")
    subprocess.run(['pip3', 'install', 'rumps', '--break-system-packages'], check=True)
    import rumps

def parse_digico_rtf(rtf_content):
    """Parse DiGiCo RTF session report and extract all channel sections"""
    
    # Decode if it's bytes
    if isinstance(rtf_content, bytes):
        rtf_content = rtf_content.decode('utf-8', errors='ignore')
    
    # Result structure with all sections
    result = {
        'inputs': [],
        'aux': [],
        'groups': [],
        'matrix': []
    }
    
    # Split into lines by \par
    lines = rtf_content.split('\\par')
    
    # State tracking
    current_section = None
    found_header = False
    
    for i, line in enumerate(lines):
        # Detect section headers
        if 'Input Channels' in line and '\\b' in line:
            current_section = 'inputs'
            found_header = False
            print(f"Found Input Channels section at line {i}")
            continue
        elif 'Aux Outputs' in line and '\\b' in line:
            current_section = 'aux'
            found_header = False
            print(f"Found Aux Outputs section at line {i}")
            continue
        elif 'Group Outputs' in line and '\\b' in line:
            current_section = 'groups'
            found_header = False
            print(f"Found Group Outputs section at line {i}")
            continue
        elif 'Matrix Outputs' in line and '\\b' in line:
            current_section = 'matrix'
            found_header = False
            print(f"Found Matrix Outputs section at line {i}")
            continue
        elif 'Matrix Inputs' in line or 'Control Groups' in line:
            # End of sections we care about
            current_section = None
            continue
        
        # Skip header line in each section
        if current_section and not found_header:
            if 'name' in line.lower():
                found_header = True
                print(f"Found {current_section} header line, skipping")
                continue
        
        # Parse channel lines
        if current_section and found_header:
            parts = line.split('\\tab')
            
            if len(parts) >= 2:
                # Get channel number - different formats for different sections
                # Inputs: "1", "1s"
                # Aux: "A1", "A1s"
                # Groups: "G1", "G1s"  
                # Matrix: "M1", "M1s"
                if current_section == 'inputs':
                    channel_match = re.search(r'^(\d+s?)\s*$', parts[0].strip())
                else:
                    # Aux, Groups, Matrix have letter prefix
                    channel_match = re.search(r'^([AGM]\d+s?)\s*$', parts[0].strip())
                
                if channel_match:
                    channel_num = channel_match.group(1).strip()
                    channel_name = parts[1].strip()

                    # Clean up name
                    channel_name = re.sub(r'\s+', ' ', channel_name)

                    if channel_name and channel_name.lower() not in ['name', '']:
                        _digico_default_pats = {
                            'inputs': re.compile(r'^Ch \d+$'),
                            'aux':    re.compile(r'^Aux \d+$'),
                            'groups': re.compile(r'^Grp \d+$'),
                            'matrix': re.compile(r'^Matrix \d+$'),
                        }
                        is_default = bool(_digico_default_pats.get(current_section, re.compile(r'^$')).match(channel_name))
                        result[current_section].append({
                            'number': channel_num,
                            'name': channel_name,
                            'type': current_section,
                            'is_default': is_default,
                        })
                        if len(result[current_section]) <= 3:
                            print(f"  [{current_section}] {channel_num}: {channel_name}")
    
    # Print summary
    print(f"\n=== PARSE SUMMARY ===")
    print(f"Inputs: {len(result['inputs'])}")
    print(f"Aux: {len(result['aux'])}")
    print(f"Groups: {len(result['groups'])}")
    print(f"Matrix: {len(result['matrix'])}")

    return result


def parse_rivage_pm_show_file(file_content):
    """Parse a Yamaha Rivage PM .RIVAGEPM show file and extract channel sections.

    The file is a Yamaha MBDF (Multi-Block Data Format) container.  The mixing
    data lives in the first zlib-compressed block that contains the b'EN00/mix'
    marker.  Inside that block a binary schema section (COL0 / PR entries)
    precedes a raw data section whose layout is described by the schema.
    """

    if isinstance(file_content, str):
        file_content = file_content.encode('latin-1')

    # ── 1. Locate and decompress the mixing block ──────────────────────────
    raw = None
    for pos in range(0, len(file_content) - 2):
        if file_content[pos:pos+2] not in (b'\x78\x01', b'\x78\x9c', b'\x78\xda'):
            continue
        try:
            candidate = zlib.decompress(file_content[pos:pos+200000])
            if b'EN00/mix' in candidate[:256]:
                raw = candidate
                break
        except Exception:
            continue

    if raw is None:
        return {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    # ── 2. Walk COL0/PR schema entries to find data-section start ──────────
    schema = []
    pos = 196  # skip file/block headers
    while pos < len(raw) - 32:
        if raw[pos:pos+4] == b'COL0':
            name_b = raw[pos+4:pos+28]
            name = name_b[:name_b.find(0) if 0 in name_b else 24].decode('ascii', errors='replace')
            vals = struct.unpack('<5I', raw[pos+28:pos+48])
            schema.append({'kind': 'COL0', 'name': name, 'v': vals})
            pos += 48
        elif raw[pos:pos+3] == b'PR ':
            pos += 32
        else:
            break

    data_start = pos

    # ── 3. Build a map of top-level channel sections ───────────────────────
    # For each section (InputChannel, Mix, …) we need:
    #   data_offset  – byte offset from data_start to this section's records
    #   rec_size     – bytes per record
    #   count        – number of records
    #   name_offset  – byte offset of the Name field within each record
    #                  (equals v[2] of the immediately-following COL0Label)
    TARGET_SECTIONS = ('InputChannel', 'Mix', 'Matrix', 'Stereo')
    sections = {}
    for i, entry in enumerate(schema):
        if entry['kind'] != 'COL0' or entry['name'] not in TARGET_SECTIONS:
            continue
        v = entry['v']
        if v[4] < 1:
            continue
        # Look ahead for the next COL0Label to get the name field offset
        name_offset = None
        for j in range(i + 1, min(i + 60, len(schema))):
            if schema[j]['kind'] == 'COL0' and schema[j]['name'] == 'Label':
                name_offset = schema[j]['v'][2]
                break
        if name_offset is None:
            continue
        if entry['name'] not in sections:  # keep first occurrence only
            sections[entry['name']] = {
                'data_offset': v[2],
                'rec_size':    v[3],
                'count':       v[4],
                'name_offset': name_offset,
            }

    # ── 4. Extract channel names ───────────────────────────────────────────
    def read_names(sect_name):
        info = sections.get(sect_name)
        if not info:
            return []
        sect_start = data_start + info['data_offset']
        names = []
        seen = set()
        for i in range(info['count']):
            p = sect_start + i * info['rec_size'] + info['name_offset']
            nb = raw[p:p+64]
            null = nb.find(0)
            name = nb[:null if null >= 0 else 64].decode('ascii', errors='replace').strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        return names

    input_names  = read_names('InputChannel')
    mix_names    = read_names('Mix')
    matrix_names = read_names('Matrix')
    stereo_names = read_names('Stereo')

    # ── 5. Build the standard result structure ─────────────────────────────
    result = {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    _riv_inp = re.compile(r'^ch\d+$')
    _riv_aux = re.compile(r'^MX\d+$')
    _riv_mtx = re.compile(r'^MT\d+$')

    for i, name in enumerate(input_names):
        result['inputs'].append({'number': str(i + 1), 'name': name, 'type': 'inputs', 'is_default': bool(_riv_inp.match(name))})

    for i, name in enumerate(mix_names):
        result['aux'].append({'number': f'MX{i+1}', 'name': name, 'type': 'aux', 'is_default': bool(_riv_aux.match(name))})

    for i, name in enumerate(stereo_names):
        result['groups'].append({'number': f'ST{i+1}', 'name': name, 'type': 'groups', 'is_default': False})

    for i, name in enumerate(matrix_names):
        result['matrix'].append({'number': f'MT{i+1}', 'name': name, 'type': 'matrix', 'is_default': bool(_riv_mtx.match(name))})

    print(f"\n=== RIVAGE PARSE SUMMARY ===")
    print(f"Inputs: {len(result['inputs'])}")
    print(f"Mix (Aux): {len(result['aux'])}")
    print(f"Stereo (Groups): {len(result['groups'])}")
    print(f"Matrix: {len(result['matrix'])}")

    return result


def parse_dlive_show_file(file_content):
    """Parse an Allen & Heath dLive .tar.gz show file and extract channel sections.

    The show file is a tar.gz archive containing a Show/ directory.
    Channel names live in Show/Scenes/StageBoxScene65535.tar.gz, which itself
    contains a .dat binary file with "Name Colour Manager" sections.
    Each channel record is 9 bytes: 1 byte color index + 8 bytes null-padded name.
    """

    if isinstance(file_content, str):
        file_content = file_content.encode('latin-1')

    # ── 1. Open outer .tar.gz and extract StageBoxScene65535.tar.gz ───────
    try:
        outer = tarfile.open(fileobj=io.BytesIO(file_content), mode='r:gz')
    except Exception:
        return {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    scene_gz = None
    input_config_raw = None
    for member in outer.getmembers():
        if 'StageBoxScene65535' in member.name and member.name.endswith('.tar.gz'):
            f = outer.extractfile(member)
            if f:
                scene_gz = f.read()
        elif member.name.endswith('InputConfig/InputConfig.dat'):
            f = outer.extractfile(member)
            if f:
                input_config_raw = f.read()
    outer.close()

    if scene_gz is None:
        return {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    # Parse stereo pair assignments from InputConfig.dat.
    # Format: line 0 = version, lines 1–N = one entry per pair ("1"=stereo, "0"=mono).
    avantis_stereo_pairs: set[int] = set()
    if input_config_raw:
        lines = input_config_raw.decode('utf-8', errors='ignore').strip().split('\n')
        for pair_idx, line in enumerate(lines[1:]):
            if line.strip() == '1':
                avantis_stereo_pairs.add(pair_idx)

    # ── 2. Open inner .tar.gz and extract the .dat file ───────────────────
    try:
        inner = tarfile.open(fileobj=io.BytesIO(scene_gz), mode='r:gz')
    except Exception:
        return {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    dat = None
    for member in inner.getmembers():
        if member.name.endswith('.dat'):
            f = inner.extractfile(member)
            if f:
                dat = f.read()
            break
    inner.close()

    if dat is None:
        return {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    # ── 3. Find all Name Colour Manager sections and parse records ─────────
    # ALL_SECTIONS is used purely for boundary detection — every section header
    # in the file must be listed so gaps between wanted sections are accurate.
    # SECTION_MAP controls which sections we actually extract.
    ALL_SECTIONS = [
        b'#Input Channel Name Colour Manager',
        b'Input Channel Name Colour Manager',   # Avantis (no # prefix)
        b'Mono Group Channel Name Colour Manager',
        b'Stereo Group Channel Name Colour Manager',
        b'Mono Aux Channel Name Colour Manager',
        b'Stereo Aux Channel Name Colour Manager',
        b'Mono FX Send Channel Name Colour Manager',
        b'Stereo FX Send Channel Name Colour Manager',
        b'Stereo AHFX Send Channel Name Colour Manager',
        b'Main Channel Name Colour Manager',
        b'Mono Matrix Channel Name Colour Manager',
        b'Stereo Matrix Channel Name Colour Manager',
        b'FX Return Channel Name Colour Manager',
        b'AHFX Return Channel Name Colour Manager',
        b'DCA Channel Name Colour Manager',
        b'Monitor Channel Name Colour Manager',
    ]
    SECTION_MAP = {
        b'#Input Channel Name Colour Manager':        'inputs',
        b'Input Channel Name Colour Manager':         'inputs',   # Avantis
        b'Mono Group Channel Name Colour Manager':    'groups',
        b'Stereo Group Channel Name Colour Manager':  'groups',
        b'Mono Aux Channel Name Colour Manager':      'aux',
        b'Stereo Aux Channel Name Colour Manager':    'aux',
        b'Main Channel Name Colour Manager':          'groups',
        b'Mono Matrix Channel Name Colour Manager':   'matrix',
        b'Stereo Matrix Channel Name Colour Manager': 'matrix',
        b'Monitor Channel Name Colour Manager':       'aux',
    }

    # Build sorted list of (name_pos, data_start, section_name) for every section found
    all_found = []
    for section_name in ALL_SECTIONS:
        pos = dat.find(section_name + b'\x00')
        if pos >= 0:
            data_start = pos + len(section_name) + 1
            all_found.append((pos, data_start, section_name))
    all_found.sort()

    STEREO_SECTIONS = {
        b'Stereo Group Channel Name Colour Manager',
        b'Stereo Aux Channel Name Colour Manager',
        b'Stereo Matrix Channel Name Colour Manager',
        b'Main Channel Name Colour Manager',
    }

    # Label used when a channel has only a numeric default name (e.g. "1", "32")
    DEFAULT_LABEL = {
        b'#Input Channel Name Colour Manager':        'Input',
        b'Input Channel Name Colour Manager':         'Input',   # Avantis
        b'Mono Group Channel Name Colour Manager':    'Mono Grp',
        b'Stereo Group Channel Name Colour Manager':  'Stereo Grp',
        b'Mono Aux Channel Name Colour Manager':      'Mono Aux',
        b'Stereo Aux Channel Name Colour Manager':    'Stereo Aux',
        b'Main Channel Name Colour Manager':          'Main',
        b'Mono Matrix Channel Name Colour Manager':   'Mono Mtx',
        b'Stereo Matrix Channel Name Colour Manager': 'Stereo Mtx',
        b'Monitor Channel Name Colour Manager':       'Monitor',
        b'DCA Channel Name Colour Manager':           'DCA',
    }

    result = {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}
    counters = {'inputs': 0, 'aux': 0, 'groups': 0, 'matrix': 0}
    prefix_map = {'inputs': '', 'aux': 'AUX', 'groups': 'GRP', 'matrix': 'MTX'}

    for idx, (pos, data_start, section_name) in enumerate(all_found):
        category = SECTION_MAP.get(section_name)
        if category is None:
            continue  # boundary only — don't extract

        # Use the very next section (wanted or not) as the boundary
        next_pos = all_found[idx + 1][0] if idx + 1 < len(all_found) else data_start + 9 * 256
        count = min((next_pos - data_start) // 9, 256)
        default_label = DEFAULT_LABEL.get(section_name, '')
        section_idx = 0

        # Collect raw names for this section first (needed for stereo pair look-ahead)
        raw_names = []
        for i in range(count):
            rec = dat[data_start + i * 9: data_start + i * 9 + 9]
            if len(rec) < 9:
                break
            name_bytes = rec[1:9]
            null = name_bytes.find(0)
            raw = name_bytes[:null if null >= 0 else 8]
            try:
                raw_str = raw.decode('ascii').strip()
            except UnicodeDecodeError:
                break
            if not raw_str or not raw_str.isprintable():
                break
            raw_names.append(raw_str)

        # Detect stereo input pairs on Avantis (no # prefix) using InputConfig.dat.
        # Falls back to name heuristics if InputConfig.dat was not found in the archive.
        skip_indices = set()
        stereo_indices = set()
        if category == 'inputs' and section_name == b'Input Channel Name Colour Manager':
            if avantis_stereo_pairs:
                for i in range(0, len(raw_names), 2):
                    pair_idx = i // 2
                    if pair_idx in avantis_stereo_pairs:
                        stereo_indices.add(i)
                        if i + 1 < len(raw_names):
                            skip_indices.add(i + 1)
            else:
                for i in range(0, len(raw_names) - 1, 2):
                    l_name = raw_names[i]
                    r_name = raw_names[i + 1]
                    r_ch_num = str(i + 2)
                    if not l_name.isdigit() and (r_name == r_ch_num or '/' in l_name or r_name == l_name + ' R'):
                        stereo_indices.add(i)
                        skip_indices.add(i + 1)

        for i, raw_name in enumerate(raw_names):
            if i in skip_indices:
                continue

            section_idx += 1
            name = raw_name
            if name.isdigit():
                name = f'{default_label} {section_idx}'

            counters[category] += 1
            n = counters[category]
            is_section_stereo = section_name in STEREO_SECTIONS
            is_pair_stereo = i in stereo_indices
            if category == 'inputs':
                number = f'{n}s' if is_pair_stereo else str(n)
            elif is_section_stereo:
                number = f'{prefix_map[category]}{n}s'
            else:
                number = f'{prefix_map[category]}{n}'
            is_default = raw_name.isdigit()
            result[category].append({'number': number, 'name': name, 'type': category, 'is_default': is_default})

    print(f"\n=== DLIVE PARSE SUMMARY ===")
    print(f"Inputs: {len(result['inputs'])}")
    print(f"Aux: {len(result['aux'])}")
    print(f"Groups: {len(result['groups'])}")
    print(f"Matrix: {len(result['matrix'])}")

    return result


def parse_m32_show_file(file_content):
    """Parse a Behringer X32 / Midas M32 .scn scene file and extract channel sections.

    The file is plain text with OSC-style paths.  Channel config lines follow the pattern:
      /ch/01/config   "Name" icon COLOR channel_num   (32 input channels)
      /auxin/01/config "Name" icon COLOR channel_num  (8 aux inputs)
      /bus/01/config  "Name" icon COLOR               (16 mix buses)
      /mtx/01/config  "Name" icon COLOR               (6 matrix outputs)
    """
    if isinstance(file_content, bytes):
        file_content = file_content.decode('utf-8', errors='ignore')

    result = {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}
    pattern = re.compile(r'^/(ch|auxin|bus|mtx)/(\d+)/config\s+"([^"]*)"')

    for line in file_content.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        section, num_str, name = m.group(1), m.group(2), m.group(3)
        num = int(num_str)

        if section == 'ch':
            is_default = not name
            name = name if name else f'Ch {num:02d}'
            result['inputs'].append({'number': str(num), 'name': name, 'type': 'inputs', 'color': None, 'is_default': is_default})
        elif section == 'auxin':
            is_default = not name
            name = name if name else f'Aux In {num}'
            result['inputs'].append({'number': f'AUX{num}', 'name': name, 'type': 'inputs', 'color': None, 'is_default': is_default})
        elif section == 'bus':
            is_default = not name
            name = name if name else f'Bus {num:02d}'
            result['aux'].append({'number': f'BUS{num}', 'name': name, 'type': 'aux', 'color': None, 'is_default': is_default})
        elif section == 'mtx':
            is_default = not name
            name = name if name else f'Mtx {num}'
            result['matrix'].append({'number': f'MTX{num}', 'name': name, 'type': 'matrix', 'color': None, 'is_default': is_default})

    print(f"\n=== M32/X32 PARSE SUMMARY ===")
    print(f"Inputs: {len(result['inputs'])}")
    print(f"Aux (Buses): {len(result['aux'])}")
    print(f"Matrix: {len(result['matrix'])}")

    return result


def parse_s6l_show_file(file_content):
    """Parse an Avid S6L / VENUE .dsh show file and extract channel sections.

    The file uses Digidesign Storage binary format.  Each strip is stored as:
      \nStrip\x00\x0d + 5 bytes + name (null-terminated)
    The strip type (input vs bus) is determined by the nearest preceding marker:
      \nInputStrip\x00       → input channel
      \nAudioMasterStrip\x00 / \nBusMasterStrip\x00 → bus / aux
    Only the first snapshot is parsed (duplicates signal a new snapshot).
    """
    if isinstance(file_content, str):
        file_content = file_content.encode('latin-1')

    result = {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}
    strip_name_pat = re.compile(rb'\nStrip\x00\x0d.{5}([\x20-\x7e]+)\x00')
    seen_inputs = set()
    seen_buses = set()
    inp_num = aux_num = 0

    for m in strip_name_pat.finditer(file_content):
        name = m.group(1).decode('ascii', errors='replace').strip()
        if not name:
            continue

        pos = m.start()
        chunk = file_content[max(0, pos - 2000):pos]
        inp_dist = len(chunk) - chunk.rfind(b'\nInputStrip\x00')
        bus_dist = len(chunk) - max(
            chunk.rfind(b'\nAudioMasterStrip\x00'),
            chunk.rfind(b'\nBusMasterStrip\x00')
        )
        is_input = inp_dist < bus_dist

        if is_input:
            if name in seen_inputs:
                break  # repeated input name = second snapshot, stop
            seen_inputs.add(name)
            inp_num += 1
            result['inputs'].append({'number': str(inp_num), 'name': name, 'type': 'inputs', 'color': None})
        else:
            if name in seen_buses:
                continue  # skip duplicate bus names
            seen_buses.add(name)
            aux_num += 1
            result['aux'].append({'number': f'BUS{aux_num}s', 'name': name, 'type': 'aux', 'color': None})

    print(f"\n=== S6L PARSE SUMMARY ===")
    print(f"Inputs: {len(result['inputs'])}")
    print(f"Aux/Bus: {len(result['aux'])}")

    return result


def parse_wing_show_file(file_content):
    """Parse a Behringer Wing .snap (or .show) file and extract channel sections.

    Channel names are stored on the input source (ae_data.io.in[grp][n].name).
    Each channel strip (ae_data.ch) and aux strip (ae_data.aux) has its own name
    (often empty) plus a conn pointer {grp, in} that resolves to the input source.
    We prefer the strip name when set, otherwise fall back to the source name.

      ae_data.ch   — 40 input channel strips → inputs
      ae_data.aux  — 8 aux input strips      → inputs
      ae_data.bus  — 16 mix buses            → aux
      ae_data.main — main L/R/sub/fill       → groups
      ae_data.mtx  — 8 matrix outputs        → matrix
    """
    if isinstance(file_content, bytes):
        file_content = file_content.decode('utf-8', errors='ignore')

    try:
        data = json.loads(file_content)
    except Exception:
        return {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    ae = data.get('ae_data') or data
    result = {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    # Build lookups for input source names and modes: (grp, num_str) → name / mode
    io_in = ae.get('io', {}).get('in', {})
    source_names = {}
    source_modes = {}
    for grp, entries in io_in.items():
        if isinstance(entries, dict):
            for num_str, v in entries.items():
                if isinstance(v, dict):
                    source_names[(grp, num_str)] = v.get('name', '').strip()
                    source_modes[(grp, num_str)] = v.get('mode', 'M')

    def resolve_name(strip, default_label, num):
        name = strip.get('name', '').strip()
        if not name:
            conn = strip.get('in', {}).get('conn', {})
            grp = conn.get('grp', '')
            src_in = str(conn.get('in', ''))
            name = source_names.get((grp, src_in), '')
        if name:
            return name, False
        return f'{default_label} {num:02d}', True

    def get_src_mode(strip):
        conn = strip.get('in', {}).get('conn', {})
        return source_modes.get((conn.get('grp', ''), str(conn.get('in', ''))), 'M')

    # 40 input channel strips — stereo pairs share the same source name with mode='ST'
    last_stereo_name = None
    for k, v in sorted(ae.get('ch', {}).items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
        if not isinstance(v, dict):
            continue
        num = int(k)
        name, is_default = resolve_name(v, 'Ch', num)
        src_mode = get_src_mode(v)
        if src_mode == 'ST':
            if name == last_stereo_name:
                last_stereo_name = None
                continue  # R side of stereo pair already added
            last_stereo_name = name
            result['inputs'].append({'number': f'{num}s', 'name': name, 'type': 'inputs', 'color': None, 'is_default': is_default})
        else:
            last_stereo_name = None
            result['inputs'].append({'number': str(num), 'name': name, 'type': 'inputs', 'color': None, 'is_default': is_default})

    # 8 aux input strips
    for k, v in sorted(ae.get('aux', {}).items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
        if not isinstance(v, dict):
            continue
        num = int(k)
        name, is_default = resolve_name(v, 'Aux', num)
        result['inputs'].append({'number': f'AUX{num}', 'name': name, 'type': 'inputs', 'color': None, 'is_default': is_default})

    # Mix buses, mains, matrix
    for k, v in sorted(ae.get('bus', {}).items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
        if isinstance(v, dict):
            num = int(k)
            name_raw = v.get('name', '').strip()
            is_default = not name_raw
            name = name_raw or f'Bus {num:02d}'
            is_stereo = not v.get('busmono', True)
            number = f'BUS{num}s' if is_stereo else f'BUS{num}'
            result['aux'].append({'number': number, 'name': name, 'type': 'aux', 'color': None, 'is_default': is_default})

    for k, v in sorted(ae.get('main', {}).items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
        if isinstance(v, dict):
            num = int(k)
            name_raw = v.get('name', '').strip()
            is_default = not name_raw
            name = name_raw or f'Main {num}'
            result['groups'].append({'number': f'MAIN{num}', 'name': name, 'type': 'groups', 'color': None, 'is_default': is_default})

    for k, v in sorted(ae.get('mtx', {}).items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
        if isinstance(v, dict):
            num = int(k)
            name_raw = v.get('name', '').strip()
            is_default = not name_raw
            name = name_raw or f'Mtx {num:02d}'
            result['matrix'].append({'number': f'MTX{num}', 'name': name, 'type': 'matrix', 'color': None, 'is_default': is_default})

    print(f"\n=== WING PARSE SUMMARY ===")
    print(f"Inputs (ch+aux): {len(result['inputs'])}")
    print(f"Bus (Aux): {len(result['aux'])}")
    print(f"Main (Groups): {len(result['groups'])}")
    print(f"Matrix: {len(result['matrix'])}")

    return result


def parse_sq_show_file(file_content):
    """Parse an Allen & Heath SQ scene .DAT file and extract channel sections.

    SQ-MixPad saves scenes as 128 KB binary .DAT files (SCENE000.DAT, etc.).
    Channel records are 336 bytes each starting at offset 880.
    Each record: [4-byte header][8-byte null-padded name][324 bytes of data]

    Confirmed channel layout (0-indexed record numbers):
       0–39:  Mono inputs 1–40
      40–47:  Stereo input pairs: even = L (keep), odd = R (skip)
      72–79:  Group buses (8 stereo groups)
      88–91:  Stereo aux sends (IEM-type)
      92–95:  Mono aux sends (wedge-type)
    108–110:  Stereo matrix outputs MTX 1–3 (names not stored; use defaults)
    """
    if isinstance(file_content, str):
        file_content = file_content.encode('latin-1')

    if len(file_content) != 131072 or file_content[:2] != b'\xa1\x00':
        return {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    RECORD_OFFSET = 880
    RECORD_STRIDE = 336

    def read_name(rec_idx):
        off = RECORD_OFFSET + rec_idx * RECORD_STRIDE + 4
        raw = file_content[off:off + 8]
        null = raw.find(0)
        try:
            return raw[:null if null >= 0 else 8].decode('ascii').strip()
        except UnicodeDecodeError:
            return ''

    # Stereo flags: bitmask starting at file offset 80, indexed by internal bus ID.
    # bus_id maps to byte (80 + bus_id // 8), bit (bus_id % 8). 1 = stereo, 0 = mono.
    def is_stereo(bus_id):
        byte_idx = 80 + bus_id // 8
        bit = bus_id % 8
        return bool((file_content[byte_idx] >> bit) & 1)

    result = {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}
    input_n = 0

    for i in range(48):
        in_stereo_zone = (i >= 40)
        if in_stereo_zone and i % 2 == 1:
            continue  # R side of stereo pair — skip
        input_n += 1
        raw = read_name(i)
        is_default = not raw
        name = raw or f'Input {input_n}'
        number = f'{input_n}s' if in_stereo_zone else str(input_n)
        result['inputs'].append({'number': number, 'name': name, 'type': 'inputs', 'is_default': is_default})

    # Groups: records 72-79, bus IDs 64-71
    grp_n = 0
    for i in range(72, 80):
        raw = read_name(i)
        is_default = not raw
        grp_n += 1
        stereo = is_stereo(64 + (i - 72))
        number = f'GRP{grp_n}s' if stereo else f'GRP{grp_n}'
        name = raw or f'Group {grp_n}'
        result['groups'].append({'number': number, 'name': name, 'type': 'groups', 'is_default': is_default})

    # Aux: records 88-95, bus IDs 80-87 (stereo IEM or mono wedge per bitmask)
    aux_n = 0
    for i in range(88, 96):
        raw = read_name(i)
        is_default = not raw
        aux_n += 1
        stereo = is_stereo(80 + (i - 88))
        number = f'AUX{aux_n}s' if stereo else f'AUX{aux_n}'
        name = raw or f'Aux {aux_n}'
        result['aux'].append({'number': number, 'name': name, 'type': 'aux', 'is_default': is_default})

    # Matrix: records 107-112, bus IDs 107-109.
    # Each stereo pair (bus 107-109) has a secondary mono slot (records 110-112).
    # When a pair is stereo → 1 channel; when mono → 2 channels (primary + secondary).
    mtx_n = 0
    for pair in range(3):
        mtx_n += 1
        stereo = is_stereo(107 + pair)
        raw = read_name(107 + pair)
        name = raw or f'MTX {mtx_n}'
        number = f'MTX{mtx_n}s' if stereo else f'MTX{mtx_n}'
        result['matrix'].append({'number': number, 'name': name, 'type': 'matrix', 'is_default': not raw})
    for pair in range(3):
        if not is_stereo(107 + pair):
            mtx_n += 1
            raw = read_name(110 + pair)
            name = raw or f'MTX {mtx_n}'
            result['matrix'].append({'number': f'MTX{mtx_n}', 'name': name, 'type': 'matrix', 'is_default': not raw})

    print(f"\n=== SQ PARSE SUMMARY ===")
    print(f"Inputs: {len(result['inputs'])}")
    print(f"Aux: {len(result['aux'])}")
    print(f"Groups: {len(result['groups'])}")
    return result


def parse_dm7_show_file(file_content):
    """Parse a Yamaha DM7 .dm7f project file and extract channel sections.

    The file is a binary MBDF (Multi-Band Data Format) container.  It holds
    multiple zlib-compressed blocks, each starting with '#YAMAHA MBDFBackup'.
    The first block whose schema header contains 'Mixing' stores the mixing
    state, including channel names packed as fixed-width records.

    Record layouts (all counts and strides are read from COL0 schema headers):
      Input channels (COL0InputChannel) — anchor b'STEREO\\x00\\x00', name at +8
      Mix buses      (COL0Mix)          — anchor b'VARI', name at +11
      Matrix outputs (COL0Matrix)       — 3-zero prefix, name at +3
      Stereo buses   (COL0Stereo)       — 0x02 prefix, name at +1 (deduplicated)
    """
    if isinstance(file_content, str):
        file_content = file_content.encode('latin-1')

    result = {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    # ── Find the Mixing MBDFBackup block ──────────────────────────────────────
    mixing_data = None
    for i in range(len(file_content) - 1):
        b0, b1 = file_content[i], file_content[i + 1]
        if b0 == 0x78 and b1 in (0x01, 0x9C, 0xDA):
            try:
                dec = zlib.decompress(file_content[i:])
                # Mixing block has '#MMS FIELD\x00\x00Mixing' at offset 72
                if dec[72:90] == b'#MMS FIELD\x00\x00Mixing' and b'COL0InputChannel' in dec[:600]:
                    mixing_data = dec
                    break
            except Exception:
                pass

    if mixing_data is None:
        return result

    # ── Input channels (STEREO records) ───────────────────────────────────────
    col0_pos = mixing_data.find(b'COL0InputChannel')
    if col0_pos < 0:
        return result

    inp_record_size = struct.unpack_from('<I', mixing_data, col0_pos + 40)[0]
    inp_count       = struct.unpack_from('<I', mixing_data, col0_pos + 44)[0]

    stereo_off = mixing_data.find(b'STEREO\x00\x00')
    if stereo_off < 0 or inp_record_size == 0:
        return result

    inp_num = 0
    for i in range(inp_count):
        name_off = stereo_off + 8 + i * inp_record_size
        if name_off + 64 > len(mixing_data):
            break
        if mixing_data[name_off - 8:name_off] != b'STEREO\x00\x00':
            break
        name = mixing_data[name_off:name_off + 64].rstrip(b'\x00').decode('ascii', errors='replace').strip()
        is_default = not name
        if not name:
            name = f'Ch {inp_num + 1:02d}'
        inp_num += 1
        result['inputs'].append({'number': str(inp_num), 'name': name, 'type': 'inputs', 'color': None, 'is_default': is_default})

    # ── Mix buses (VARI records) ───────────────────────────────────────────────
    VARI_STRIDE   = 647
    VARI_NAME_OFF = 11
    # STEREO records start 10 bytes before the STEREO marker; account for that
    # offset when computing where the block ends before the VARI section begins.
    stereo_block_start = stereo_off - 10
    vari_search_start = stereo_block_start + inp_count * inp_record_size
    vari_off = mixing_data.find(b'VARI', vari_search_start)

    aux_num = 0
    while vari_off >= 0 and vari_off + VARI_NAME_OFF + 64 <= len(mixing_data):
        if mixing_data[vari_off:vari_off + 4] != b'VARI':
            break
        name_off = vari_off + VARI_NAME_OFF
        name = mixing_data[name_off:name_off + 64].rstrip(b'\x00').decode('ascii', errors='replace').strip()
        is_default = not name
        if not name:
            name = f'MX{aux_num + 1:02d}'
        aux_num += 1
        result['aux'].append({'number': f'BUS{aux_num}', 'name': name, 'type': 'aux', 'color': None, 'is_default': is_default})
        vari_off += VARI_STRIDE
        if mixing_data[vari_off:vari_off + 4] != b'VARI':
            break
    # vari_off now points to the first byte after the VARI block
    vari_block_end = vari_off

    # ── Matrix outputs (3-zero prefix records) ───────────────────────────────
    col0_mtx = mixing_data.find(b'COL0Matrix')
    if col0_mtx >= 0:
        mtx_stride = struct.unpack_from('<I', mixing_data, col0_mtx + 40)[0]
        mtx_count  = struct.unpack_from('<I', mixing_data, col0_mtx + 44)[0]

        # Matrix block follows immediately after VARI block
        mtx_start = vari_block_end
        mtx_num = 0
        for i in range(mtx_count):
            rec_start = mtx_start + i * mtx_stride
            name_off  = rec_start + 3
            if name_off + 64 > len(mixing_data):
                break
            if mixing_data[rec_start:rec_start + 3] != b'\x00\x00\x00':
                break
            name = mixing_data[name_off:name_off + 64].rstrip(b'\x00').decode('ascii', errors='replace').strip()
            is_default = not name
            if not name:
                name = f'MT{mtx_num + 1:02d}'
            mtx_num += 1
            result['matrix'].append({'number': f'MTX{mtx_num}', 'name': name, 'type': 'matrix', 'color': None, 'is_default': is_default})

    # ── Stereo buses (0x02-prefix records, deduplicated) ─────────────────────
    col0_st = mixing_data.find(b'COL0Stereo')
    if col0_st >= 0:
        st_stride = struct.unpack_from('<I', mixing_data, col0_st + 40)[0]
        st_count  = struct.unpack_from('<I', mixing_data, col0_st + 44)[0]

        # Stereo block follows immediately after matrix block
        st_start = mtx_start + mtx_num * mtx_stride if col0_mtx >= 0 else vari_block_end
        seen_st  = set()
        grp_num  = 0
        for i in range(st_count):
            rec_start = st_start + i * st_stride
            name_off  = rec_start + 1
            if name_off + 64 > len(mixing_data):
                break
            if mixing_data[rec_start:rec_start + 1] != b'\x02':
                break
            name = mixing_data[name_off:name_off + 64].rstrip(b'\x00').decode('ascii', errors='replace').strip()
            if not name or name in seen_st:
                continue
            seen_st.add(name)
            grp_num += 1
            result['groups'].append({'number': f'ST{grp_num}s', 'name': name, 'type': 'groups', 'color': None})

    print(f"\n=== DM7 PARSE SUMMARY ===")
    print(f"Inputs: {len(result['inputs'])}")
    print(f"Mix buses (Aux): {len(result['aux'])}")
    print(f"Matrix: {len(result['matrix'])}")
    print(f"Stereo buses (Groups): {len(result['groups'])}")

    return result


def parse_lv1_show_file(file_content):
    """Parse a Waves LV1 .emo session file (SQLite 3 database).

    Channel names and types are in the snapshot_chainer table (snapshot_id=-1
    is the live/active state).  Stereo detection uses chainer.output_stem_format:
    101=stereo, 100=mono.  Channels with default placeholder names
    ("Channel N", "Group N", "Matrix N") or empty names are skipped.

    Channel types:  0=Input  1=Group  2=Aux  6=Matrix
    """
    import sqlite3, os, tempfile

    if isinstance(file_content, str):
        file_content = file_content.encode('latin-1')

    # Must be a SQLite 3 file
    if not file_content.startswith(b'SQLite format 3\x00'):
        return {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    result = {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.emo', delete=False) as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name

        db = sqlite3.connect(tmp_path)
        cur = db.cursor()

        # Counters for sequential numbering per section
        counters = {'inputs': 0, 'aux': 0, 'groups': 0, 'matrix': 0}
        prefix_map = {'inputs': '', 'aux': 'AUX', 'groups': 'GRP', 'matrix': 'MTX'}
        type_to_section = {0: 'inputs', 1: 'groups', 2: 'aux', 6: 'matrix'}

        cur.execute("""
            SELECT o.obj_type, o.obj_index, sc.name, c.num_inputs
            FROM snapshot_chainer sc
            JOIN chainer c ON c.obj_id = sc.chainer_id
            JOIN object o ON o.id = c.obj_id
            WHERE sc.snapshot_id = -1 AND o.obj_type IN (0, 1, 2, 6)
            ORDER BY o.obj_type, o.obj_index
        """)

        for obj_type, obj_index, name, num_inputs in cur.fetchall():
            section = type_to_section[obj_type]
            name = (name or '').strip()

            # Detect placeholder / unused channels (e.g. "Channel 23", "Group 4")
            default_n = obj_index + 1
            section_label = {'inputs': 'Channel', 'groups': 'Group',
                             'aux': 'Aux', 'matrix': 'Matrix'}[section]
            is_default = (not name) or (name == f'{section_label} {default_n}')
            if not name:
                name = f'{section_label} {default_n}'

            # Inputs are always mono sources on the LV1 (one physical input per strip).
            # For buses: num_inputs=2 means stereo mix, num_inputs=1 means mono.
            stereo = (num_inputs == 2) and (section != 'inputs')
            counters[section] += 1
            n = counters[section]
            pfx = prefix_map[section]

            if section == 'inputs':
                number = str(n)
            else:
                number = f'{pfx}{n}s' if stereo else f'{pfx}{n}'

            result[section].append({'number': number, 'name': name, 'type': section, 'is_default': is_default})

        db.close()

    except Exception as e:
        print(f"LV1 parse error: {e}")
        return {'inputs': [], 'aux': [], 'groups': [], 'matrix': []}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    print(f"\n=== LV1 PARSE SUMMARY ===")
    for k in ['inputs', 'aux', 'groups', 'matrix']:
        print(f"{k.capitalize()}: {len(result[k])}")

    return result


def hex_to_reaper_color(hex_color):
    """Convert #RRGGBB to REAPER PEAKCOL integer (R|G<<8|B<<16)|0x1000000"""
    hex_color = hex_color.lstrip('#')
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return (r | (g << 8) | (b << 16)) | 0x1000000


def _track_block(name, peakcol, rec_input, nchan):
    """Build a single REAPER TRACK block."""
    track_id = str(uuid.uuid4()).upper()
    return [
        '<TRACK',
        f'  NAME "{name}"',
        f'  PEAKCOL {peakcol}',
        '  BEAT -1',
        '  AUTOMODE 0',
        '  VOLPAN 1 0 -1 -1 1',
        '  MUTESOLO 0 0 0',
        '  IPHASE 0',
        '  PLAYOFFS 0 1',
        '  ISBUS 0 0',
        '  BUSCOMP 0 0 0 0 0',
        '  SHOWINMIX 1 0.6667 0.5 1 0.5 0 0 0',
        f'  REC 1 {rec_input} 1 0 0 0 0 0',
        '  VU 2',
        '  TRACKHEIGHT 0 0 0 0 0 0',
        '  INQ 0 0 0 0.5 100 0 0 100',
        f'  NCHAN {nchan}',
        '  FX 1',
        f'  TRACKID {{{track_id}}}',
        '  PERF 0',
        '  MIDIOUT -1',
        '  MAINSEND 1 0',
        '>',
    ]


def generate_reaper_track_template(channels, stereo_mode='split'):
    """Generate Reaper track template from channel list with sequential input routing."""

    template_lines = []
    hw = 0  # 0-based hardware input counter

    for channel in channels:
        name    = channel['name']
        peakcol = hex_to_reaper_color(channel['color']) if channel.get('color') else 16576
        is_stereo = channel['number'].endswith('s')

        if is_stereo and stereo_mode == 'stereo':
            # One stereo track — input encoded as 1024 + left_input_index
            template_lines.extend(_track_block(name, peakcol, 1024 + hw, 2))
            hw += 2
        elif is_stereo:
            # Two mono tracks (L then R), each consuming one input
            for suffix in [' L', ' R']:
                template_lines.extend(_track_block(name + suffix, peakcol, hw, 1))
                hw += 1
        else:
            # Single mono track
            template_lines.extend(_track_block(name, peakcol, hw, 1))
            hw += 1

    return '\n'.join(template_lines)


def find_available_port(start_port=8081, max_attempts=10):
    """Find an available port starting from start_port"""
    for port in range(start_port, start_port + max_attempts):
        try:
            # Try to bind to the port
            test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_socket.bind(('localhost', port))
            test_socket.close()
            return port
        except OSError:
            continue
    return None


class DiGiCoToReaperHandler(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        """Suppress request logging"""
        pass
    
    def do_GET(self):
        """Serve the web interface"""
        if self.path == '/':
            self.serve_html()
        elif self.path == '/heartbeat':
            # Simple heartbeat endpoint
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_error(404)
    
    def do_POST(self):
        """Handle file upload and conversion"""
        if self.path == '/convert':
            self.handle_conversion()
        elif self.path == '/generate':
            self.handle_generate()
        else:
            self.send_error(404)
    
    def serve_html(self):
        """Serve the main HTML interface"""
        html = '''
<!DOCTYPE html>
<html>
<head>
    <title>Console to Reaper</title>
    <meta charset="UTF-8">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
            background: #1a1a1a;
            padding: 40px 20px;
        }
        .container {
            max-width: 1000px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.4);
            padding: 40px;
        }
        h1 {
            color: #1d1d1f;
            margin-bottom: 10px;
            font-size: 32px;
            font-weight: 600;
        }
        .subtitle {
            color: #86868b;
            margin-bottom: 40px;
            font-size: 16px;
        }
        .credit {
            font-size: 12px;
            color: #86868b;
            margin-bottom: 15px;
            line-height: 1.5;
        }
        .credit a {
            color: #007aff;
            text-decoration: none;
        }
        .credit a:hover {
            text-decoration: underline;
        }
        .tab-bar {
            display: flex;
            align-items: center;
            gap: 4px;
            margin-bottom: 24px;
            border-bottom: 2px solid #e5e5ea;
            padding-bottom: 0;
            flex-wrap: wrap;
        }
        .tab {
            padding: 8px 16px;
            border-radius: 8px 8px 0 0;
            border: 1px solid transparent;
            border-bottom: none;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            color: #86868b;
            background: none;
            position: relative;
            bottom: -2px;
            transition: background 0.15s, color 0.15s;
            user-select: none;
            white-space: nowrap;
        }
        .tab:hover { background: #f2f2f7; color: #1d1d1f; }
        .tab.active {
            background: white;
            color: #1d1d1f;
            border-color: #e5e5ea;
            border-bottom-color: white;
        }
        .tab-name { outline: none; }
        .tab-close {
            display: inline-block;
            margin-left: 6px;
            color: #c0c0c0;
            font-size: 12px;
            line-height: 1;
            border-radius: 50%;
            width: 14px;
            height: 14px;
            text-align: center;
        }
        .tab-close:hover { background: #ffdddd; color: #c7251a; }
        .tab-add {
            padding: 6px 12px;
            border-radius: 8px;
            border: 1px dashed #c0c0c0;
            background: none;
            color: #86868b;
            cursor: pointer;
            font-size: 18px;
            line-height: 1;
            transition: background 0.15s, color 0.15s;
        }
        .tab-add:hover { background: #f2f2f7; color: #007aff; border-color: #007aff; }
        .upload-area {
            border: 3px dashed #d2d2d7;
            border-radius: 12px;
            padding: 60px 40px;
            text-align: center;
            background: #f9f9f9;
            cursor: pointer;
            transition: all 0.3s;
            margin-bottom: 30px;
        }
        .upload-area:hover {
            border-color: #007aff;
            background: #f0f8ff;
        }
        .upload-area.dragover {
            border-color: #007aff;
            background: #e6f2ff;
        }
        .upload-icon {
            font-size: 48px;
            margin-bottom: 20px;
        }
        .upload-text {
            font-size: 18px;
            color: #1d1d1f;
            margin-bottom: 10px;
        }
        .upload-subtext {
            font-size: 14px;
            color: #86868b;
        }
        input[type="file"] {
            display: none;
        }
        .preview-area {
            display: none;
            margin-top: 30px;
        }
        .preview-header {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 15px;
            color: #1d1d1f;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .select-buttons {
            display: flex;
            gap: 10px;
        }
        .select-buttons button {
            padding: 6px 12px;
            font-size: 13px;
            background: #e5e5ea;
            border: none;
            border-radius: 6px;
            cursor: pointer;
        }
        .select-buttons button:hover {
            background: #d1d1d6;
        }
        .preview-list {
            background: #f9f9f9;
            border-radius: 8px;
            padding: 20px;
            max-height: 500px;
            overflow-y: auto;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 13px;
        }
        .track-item {
            padding: 10px;
            border-bottom: 1px solid #e5e5ea;
            display: flex;
            align-items: center;
            gap: 12px;
            cursor: move;
            user-select: none;
        }
        .track-item:last-child {
            border-bottom: none;
        }
        .track-item:hover {
            background: #f0f0f0;
        }
        .track-item.dragging {
            opacity: 0.4;
        }
        .track-item.drop-above {
            border-top: 2px solid #007aff;
        }
        .track-item.drop-below {
            border-bottom: 2px solid #007aff;
        }
        .drag-handle {
            color: #86868b;
            font-size: 18px;
            cursor: grab;
        }
        .drag-handle:active {
            cursor: grabbing;
        }
        .track-checkbox {
            width: 18px;
            height: 18px;
            cursor: pointer;
        }
        .track-number {
            display: inline-block;
            width: 50px;
            color: #86868b;
            font-weight: 600;
        }
        .track-badge {
            display: inline-block;
            padding: 2px 8px;
            margin-right: 10px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .badge-inputs {
            background: #e5e5e7;
            color: #1d1d1f;
        }
        .badge-aux {
            background: #d1e7ff;
            color: #0066cc;
        }
        .badge-groups {
            background: #d4edda;
            color: #155724;
        }
        .badge-matrix {
            background: #fff3cd;
            color: #856404;
        }
        .badge-custom {
            background: #ede9fe;
            color: #7c3aed;
        }
        .track-delete {
            color: #86868b;
            background: none;
            border: none;
            cursor: pointer;
            font-size: 18px;
            padding: 0 4px;
            line-height: 1;
            border-radius: 4px;
            transition: color 0.2s, background 0.2s;
        }
        .track-delete:hover {
            color: #c7251a;
            background: #ffd3d0;
        }
        .track-item.active-highlight {
            background: #e8f0fe;
        }
        .track-item.active-highlight:hover {
            background: #dce8fd;
        }
        .bulk-bar {
            display: none;
            align-items: center;
            gap: 10px;
            padding: 10px 15px;
            background: #007aff;
            color: white;
            border-radius: 8px;
            margin-bottom: 10px;
            font-size: 14px;
            font-weight: 500;
            flex-wrap: wrap;
        }
        .bulk-bar.visible {
            display: flex;
        }
        .bulk-bar-btn {
            background: rgba(255,255,255,0.2);
            color: white;
            border: none;
            padding: 5px 12px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: background 0.15s;
        }
        .bulk-bar-btn:hover {
            background: rgba(255,255,255,0.35);
        }
        .bulk-color-swatch {
            width: 26px;
            height: 26px;
            border-radius: 6px;
            border: 2px solid rgba(255,255,255,0.6);
            cursor: pointer;
            background: white;
            position: relative;
            flex-shrink: 0;
            transition: transform 0.15s;
        }
        .bulk-color-swatch:hover {
            transform: scale(1.1);
        }
        .bulk-color-swatch input[type="color"] {
            position: absolute;
            width: 0;
            height: 0;
            opacity: 0;
            pointer-events: none;
        }
        .section-color-swatch {
            width: 18px;
            height: 18px;
            border-radius: 50%;
            border: 2px dashed #c0c0c0;
            background: transparent;
            cursor: pointer;
            position: relative;
            flex-shrink: 0;
            transition: transform 0.15s, border-color 0.2s;
        }
        .section-color-swatch:hover {
            transform: scale(1.2);
            border-color: #007aff;
        }
        .section-color-swatch.has-color {
            border: 2px solid rgba(0,0,0,0.15);
        }
        .section-color-swatch .color-clear-badge {
            display: none;
            position: absolute;
            top: -5px;
            right: -5px;
            width: 13px;
            height: 13px;
            border-radius: 50%;
            background: #c7251a;
            color: white;
            font-size: 9px;
            line-height: 13px;
            text-align: center;
            cursor: pointer;
            z-index: 10;
            font-weight: bold;
        }
        .section-color-swatch.has-color:hover .color-clear-badge {
            display: block;
        }
        .section-color-swatch input[type="color"] {
            position: absolute;
            width: 0;
            height: 0;
            opacity: 0;
            pointer-events: none;
        }
        .stereo-toggle {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 12px;
            font-size: 14px;
            color: #1d1d1f;
        }
        .stereo-toggle-label {
            font-weight: 500;
        }
        .stereo-toggle-options {
            display: flex;
            background: #f2f2f7;
            border-radius: 8px;
            padding: 3px;
        }
        .stereo-option {
            padding: 5px 14px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            color: #86868b;
            transition: background 0.15s, color 0.15s;
            user-select: none;
        }
        .stereo-option.active {
            background: white;
            color: #1d1d1f;
            box-shadow: 0 1px 3px rgba(0,0,0,0.12);
        }
        .track-color-btn {
            width: 20px;
            height: 20px;
            border-radius: 50%;
            border: 2px dashed #c0c0c0;
            cursor: pointer;
            flex-shrink: 0;
            position: relative;
            transition: transform 0.15s, border-color 0.2s;
            background: transparent;
        }
        .track-color-btn:hover {
            transform: scale(1.2);
            border-color: #007aff;
        }
        .track-color-btn.has-color {
            border: 2px solid rgba(0,0,0,0.15);
        }
        .track-color-btn input[type="color"] {
            position: absolute;
            width: 0;
            height: 0;
            opacity: 0;
            pointer-events: none;
        }
        .color-clear-badge {
            display: none;
            position: absolute;
            top: -5px;
            right: -5px;
            width: 13px;
            height: 13px;
            border-radius: 50%;
            background: #c7251a;
            color: white;
            font-size: 9px;
            line-height: 13px;
            text-align: center;
            cursor: pointer;
            z-index: 10;
            font-weight: bold;
        }
        .track-color-btn.has-color:hover .color-clear-badge {
            display: block;
        }
        .track-edit {
            color: #86868b;
            background: none;
            border: none;
            cursor: pointer;
            font-size: 14px;
            padding: 0 4px;
            line-height: 1;
            border-radius: 4px;
            opacity: 0;
            transition: opacity 0.2s, color 0.2s, background 0.2s;
        }
        .track-item:hover .track-edit {
            opacity: 1;
        }
        .track-edit:hover {
            color: #007aff;
            background: #e6f2ff;
        }
        .stereo-btn {
            font-size: 10px;
            font-weight: 600;
            padding: 2px 6px;
            border-radius: 10px;
            border: 1px solid #ccc;
            cursor: pointer;
            background: #f0f0f0;
            color: #888;
            flex-shrink: 0;
            transition: background 0.15s, color 0.15s, border-color 0.15s;
            line-height: 1.4;
        }
        .stereo-btn.is-stereo {
            background: #e3f0ff;
            color: #1a6fc4;
            border-color: #90c0f0;
        }
        .stereo-btn:hover {
            border-color: #1a6fc4;
            color: #1a6fc4;
        }
        .track-name {
            color: #1d1d1f;
            flex: 1;
        }
        .button-group {
            margin-top: 30px;
            display: flex;
            gap: 15px;
            justify-content: center;
        }
        button {
            padding: 12px 30px;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-primary {
            background: #007aff;
            color: white;
        }
        .btn-primary:hover {
            background: #0051d5;
        }
        .btn-secondary {
            background: #e5e5ea;
            color: #1d1d1f;
        }
        .btn-secondary:hover {
            background: #d1d1d6;
        }
        .message {
            padding: 15px;
            border-radius: 8px;
            margin-top: 20px;
            display: none;
        }
        .message.success {
            background: #d1f2dd;
            color: #248a3d;
            display: block;
        }
        .message.error {
            background: #ffd3d0;
            color: #c7251a;
            display: block;
        }
        .info-box {
            background: #e6f2ff;
            border-left: 4px solid #007aff;
            padding: 20px;
            border-radius: 8px;
            margin-top: 30px;
        }
        .info-box h3 {
            color: #007aff;
            margin-bottom: 10px;
            font-size: 16px;
        }
        .info-box ol {
            margin-left: 20px;
            color: #1d1d1f;
        }
        .info-box li {
            margin-bottom: 8px;
        }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.5);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal.active {
            display: flex;
        }
        .modal-content {
            background: white;
            border-radius: 12px;
            padding: 30px;
            max-width: 500px;
            width: 90%;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }
        .modal-content h2 {
            font-size: 20px;
            margin-bottom: 20px;
            color: #1d1d1f;
        }
        .modal-content input {
            width: 100%;
            padding: 12px;
            border: 1px solid #d2d2d7;
            border-radius: 6px;
            font-size: 16px;
            margin-bottom: 20px;
        }
        .modal-buttons {
            display: flex;
            gap: 10px;
            justify-content: flex-end;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="credit">
        Built by Michael Leckrone<br>
            <a href="mailto:leckroneaudio@gmail.com">leckroneaudio@gmail.com</a>
        </div>
        
        <div class="tab-bar" id="tabBar"></div>

        <h1>Console to Reaper Converter</h1>
        <p class="subtitle">Convert show files from DiGiCo, Yamaha Rivage/DM7, A&amp;H dLive/Avantis/SQ, Behringer X32/M32/Wing, Avid S6L, or Waves LV1 to Reaper track templates</p>

        <div id="uploadArea" class="upload-area" onclick="document.getElementById('fileInput').click()">
            <div class="upload-icon">📄</div>
            <div class="upload-text">Drop your show file here</div>
            <div class="upload-subtext">or click to browse &nbsp;·&nbsp; DiGiCo (.rtf) &nbsp;·&nbsp; Yamaha Rivage (.RIVAGEPM) &nbsp;·&nbsp; A&amp;H dLive/Avantis (.tar.gz) &nbsp;·&nbsp; A&amp;H SQ (.dat) &nbsp;·&nbsp; X32/M32 (.scn) &nbsp;·&nbsp; Wing (.snap) &nbsp;·&nbsp; Avid S6L (.dsh) &nbsp;·&nbsp; Yamaha DM7 (.dm7f) &nbsp;·&nbsp; Waves LV1 (.emo)</div>
        </div>

        <input type="file" id="fileInput" accept=".rtf,.RIVAGEPM,.rivagepm,.tar.gz,.scn,.snap,.dsh,.dm7f,.dat,.DAT,.emo,.EMO,application/gzip,application/x-gzip,application/x-tar,application/json" onchange="handleFile(this.files[0])">
        
        <div id="message" class="message"></div>
        
        <div style="text-align: right; margin: -15px 0 20px;">
            <button class="btn-secondary" onclick="openAddChannelModal()" style="font-size: 14px; padding: 8px 16px;">+ Add Channel Manually</button>
        </div>

        <div class="stereo-toggle">
                <span class="stereo-toggle-label">Stereo channels:</span>
                <div class="stereo-toggle-options">
                    <div class="stereo-option active" id="optSplit" onclick="setStereoMode('split')">Split to L/R Mono</div>
                    <div class="stereo-option" id="optStereo" onclick="setStereoMode('stereo')">Keep Stereo</div>
                </div>
            </div>
            <div class="button-group">
                <button class="btn-primary" onclick="downloadTemplate()">⬇️ Download Track Template</button>
                <button class="btn-secondary" onclick="downloadCSV()">📄 Export as CSV</button>
                <button class="btn-secondary" onclick="reset()">🔄 Upload New File</button>
            </div>

        <div id="previewArea" class="preview-area">
            <div class="preview-header">
                <span>Track Preview (<span id="selectedCount">0</span> of <span id="trackCount">0</span> selected)</span>
                <div class="select-buttons">
                    <button onclick="selectAll()">Select All</button>
                    <button onclick="selectNone()">Deselect All</button>
                    <button onclick="removeUnnamed()" title="Remove channels with console-default placeholder names (Cmd+Z to undo)">Remove Unnamed</button>
                    <button id="undoBtn" onclick="undo()" disabled style="opacity: 0.4;">↩ Undo</button>
                    <button onclick="openAddChannelModal()" style="background: #007aff; color: white;">+ Add Channel</button>
                </div>
            </div>
            
            <!-- Section Selection -->
            <div id="sectionSelector" style="margin: 20px 0; padding: 15px; background: #f9f9f9; border-radius: 8px;">
                <div style="font-weight: 600; margin-bottom: 5px;">Quick Select Sections:</div>
                <div style="font-size: 13px; color: #666; margin-bottom: 10px;">Check to auto-select all channels in a section. Uncheck to manually pick individual channels.</div>
                <div style="display: flex; gap: 20px; flex-wrap: wrap;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; margin: 0;">
                            <input type="checkbox" id="includeInputs" checked onchange="updateSectionPreview()" style="width: 18px; height: 18px;">
                            <span>Inputs (<span id="inputsCount">0</span>)</span>
                        </label>
                        <div class="section-color-swatch" id="swatchInputs" title="Set color for all Inputs" onclick="document.getElementById('colorInputs').click()">
                            <input type="color" id="colorInputs" onchange="applyColorToSection('inputs', this.value)">
                            <span class="color-clear-badge" onclick="event.stopPropagation(); clearColorFromSection('inputs')" title="Clear color">✕</span>
                        </div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; margin: 0;">
                            <input type="checkbox" id="includeAux" onchange="updateSectionPreview()" style="width: 18px; height: 18px;">
                            <span>Aux Outputs (<span id="auxCount">0</span>)</span>
                        </label>
                        <div class="section-color-swatch" id="swatchAux" title="Set color for all Aux Outputs" onclick="document.getElementById('colorAux').click()">
                            <input type="color" id="colorAux" onchange="applyColorToSection('aux', this.value)">
                            <span class="color-clear-badge" onclick="event.stopPropagation(); clearColorFromSection('aux')" title="Clear color">✕</span>
                        </div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; margin: 0;">
                            <input type="checkbox" id="includeGroups" onchange="updateSectionPreview()" style="width: 18px; height: 18px;">
                            <span>Group Outputs (<span id="groupsCount">0</span>)</span>
                        </label>
                        <div class="section-color-swatch" id="swatchGroups" title="Set color for all Group Outputs" onclick="document.getElementById('colorGroups').click()">
                            <input type="color" id="colorGroups" onchange="applyColorToSection('groups', this.value)">
                            <span class="color-clear-badge" onclick="event.stopPropagation(); clearColorFromSection('groups')" title="Clear color">✕</span>
                        </div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; margin: 0;">
                            <input type="checkbox" id="includeMatrix" onchange="updateSectionPreview()" style="width: 18px; height: 18px;">
                            <span>Matrix Outputs (<span id="matrixCount">0</span>)</span>
                        </label>
                        <div class="section-color-swatch" id="swatchMatrix" title="Set color for all Matrix Outputs" onclick="document.getElementById('colorMatrix').click()">
                            <input type="color" id="colorMatrix" onchange="applyColorToSection('matrix', this.value)">
                            <span class="color-clear-badge" onclick="event.stopPropagation(); clearColorFromSection('matrix')" title="Clear color">✕</span>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Bulk action bar -->
            <div id="bulkBar" class="bulk-bar">
                <span id="bulkCount">0 channels selected</span>
                <div class="bulk-color-swatch" id="bulkColorSwatch" title="Set color for all selected channels">
                    <input type="color" id="bulkColorInput" value="#ff6b6b">
                </div>
                <button class="bulk-bar-btn" onclick="clearBulkColors()">Clear Colors</button>
                <button class="bulk-bar-btn" onclick="clearActiveSelection()" style="margin-left: auto;">✕ Deselect All</button>
            </div>

            <div id="previewList" class="preview-list"></div>

            <div class="stereo-toggle">
                <span class="stereo-toggle-label">Stereo channels:</span>
                <div class="stereo-toggle-options">
                    <div class="stereo-option active" id="optSplitBottom" onclick="setStereoMode('split')">Split to L/R Mono</div>
                    <div class="stereo-option" id="optStereoBottom" onclick="setStereoMode('stereo')">Keep Stereo</div>
                </div>
            </div>
            <div class="button-group">
                <button class="btn-primary" onclick="downloadTemplate()">⬇️ Download Track Template</button>
                <button class="btn-secondary" onclick="downloadCSV()">📄 Export as CSV</button>
                <button class="btn-secondary" onclick="reset()">🔄 Upload New File</button>
            </div>
        </div>
        
        <div class="info-box">
            <h3>How to use:</h3>
            <ol>
                <li><strong>DiGiCo:</strong> Export session report from the console (.rtf file)</li>
                <li><strong>Yamaha Rivage PM:</strong> Copy the .RIVAGEPM show file from the console or Rivage PM Editor</li>
                <li><strong>Allen &amp; Heath dLive / Avantis:</strong> Export the show file from dLive Director or Avantis Director (.tar.gz)</li>
                <li><strong>Allen &amp; Heath SQ:</strong> Export a scene from SQ-MixPad or save to USB from the console (.dat)</li>
                <li><strong>Behringer X32 / Midas M32:</strong> Save a scene from the console or X32-Edit/M32-Edit (.scn)</li>
                <li><strong>Behringer Wing:</strong> Save a snapshot from the console or Wing-Edit (.snap)</li>
                <li><strong>Avid S6L / VENUE:</strong> Save a show file from the console or VENUE software (.dsh)</li>
                <li><strong>Yamaha DM7:</strong> Copy the .dm7f project file from the DM7 or DM7 Compact (.dm7f)</li>
                <li><strong>Waves LV1:</strong> Save session from LV1 software, upload the .emo file</li>
                <li>Upload or drag the file here</li>
                <li>Select/deselect channels you want to import</li>
                <li>Download the .RTrackTemplate file</li>
                <li>Open blank Reaper session</li>
                <li>Track → Insert tracks from template → Select the downloaded file (or drag it in)</li>
            </ol>
        </div>
    </div>
    
    <!-- Filename Modal -->
    <div id="filenameModal" class="modal">
        <div class="modal-content">
            <h2>Save Track Template</h2>
            <input type="text" id="filenameInput" placeholder="Enter filename" value="TrackTemplate">
            <div class="modal-buttons">
                <button class="btn-secondary" onclick="closeFilenameModal()">Cancel</button>
                <button class="btn-primary" onclick="confirmDownload()">Download</button>
            </div>
        </div>
    </div>
    
    <!-- Add Channel Modal -->
    <div id="addChannelModal" class="modal">
        <div class="modal-content">
            <h2>Add Channel</h2>
            <div style="margin-bottom: 15px;">
                <label style="display: block; font-weight: 500; margin-bottom: 6px; color: #1d1d1f;">Channel Name</label>
                <input type="text" id="newChannelName" placeholder="e.g. Kick Drum">
            </div>
            <div style="display: flex; gap: 12px; margin-bottom: 15px;">
                <div style="flex: 1;">
                    <label style="display: block; font-weight: 500; margin-bottom: 6px; color: #1d1d1f;">Type</label>
                    <select id="newChannelType" style="width: 100%; padding: 12px; border: 1px solid #d2d2d7; border-radius: 6px; font-size: 16px; margin-bottom: 0;">
                        <option value="inputs">Input</option>
                        <option value="aux">Aux</option>
                        <option value="groups">Group</option>
                        <option value="matrix">Matrix</option>
                        <option value="custom">Custom</option>
                    </select>
                </div>
                <div style="width: 90px;">
                    <label style="display: block; font-weight: 500; margin-bottom: 6px; color: #1d1d1f;">Quantity</label>
                    <input type="number" id="newChannelQty" value="1" min="1" max="128" style="width: 100%; padding: 12px; border: 1px solid #d2d2d7; border-radius: 6px; font-size: 16px; margin-bottom: 0;">
                </div>
            </div>
            <label style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px; cursor: pointer;">
                <input type="checkbox" id="newChannelStereo" style="width: 18px; height: 18px; margin-bottom: 0;">
                <span>Stereo channel</span>
            </label>
            <div id="newChannelQtyHint" style="font-size: 12px; color: #86868b; margin-bottom: 20px;">e.g. "Mic" × 3 → Mic 1, Mic 2, Mic 3</div>
            <div class="modal-buttons">
                <button class="btn-secondary" onclick="closeAddChannelModal()">Cancel</button>
                <button class="btn-primary" onclick="addCustomChannel()">Add Channel</button>
            </div>
        </div>
    </div>

    <!-- Disconnect Overlay -->
    <div id="disconnectOverlay" style="display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.85); z-index: 10000; align-items: center; justify-content: center;">
        <div style="background: white; padding: 40px; border-radius: 12px; text-align: center; max-width: 400px;">
            <div style="font-size: 48px; margin-bottom: 20px;">⚠️</div>
            <h2 style="margin-bottom: 10px; color: #1d1d1f;">Server Disconnected</h2>
            <p style="color: #86868b; margin-bottom: 20px;">The Console to Reaper converter has been closed.</p>
            <p style="color: #86868b; font-size: 14px;">You can close this tab or restart the app to continue.</p>
        </div>
    </div>
    
    <script>
        // ── Tab / Session management ────────────────────────────────────────
        function newSessionState(name) {
            return {
                name,
                parsedSections: { inputs: [], aux: [], groups: [], matrix: [] },
                currentCombinedChannels: [],
                selectedChannels: new Set(),
                customChannelCount: 0,
                undoStack: [],
                activeChannels: new Set(),
                lastActiveIdx: null,
                sectionChecks: { inputs: true, aux: false, groups: false, matrix: false },
            };
        }

        let sessions = [newSessionState('Session 1')];
        let activeTab = 0;

        function getTab() { return sessions[activeTab]; }

        // Proxy globals so all existing code keeps working without changes
        let parsedSections,
            currentCombinedChannels,
            selectedChannels,
            customChannelCount,
            undoStack,
            activeChannels,
            lastActiveIdx;

        function loadTabState() {
            const s = getTab();
            parsedSections          = s.parsedSections;
            currentCombinedChannels = s.currentCombinedChannels;
            selectedChannels        = s.selectedChannels;
            customChannelCount      = s.customChannelCount;
            undoStack               = s.undoStack;
            activeChannels          = s.activeChannels;
            lastActiveIdx           = s.lastActiveIdx;
            // Restore section checkboxes
            document.getElementById('includeInputs').checked = s.sectionChecks.inputs;
            document.getElementById('includeAux').checked    = s.sectionChecks.aux;
            document.getElementById('includeGroups').checked = s.sectionChecks.groups;
            document.getElementById('includeMatrix').checked = s.sectionChecks.matrix;
        }

        function saveTabState() {
            const s = getTab();
            s.parsedSections          = parsedSections;
            s.currentCombinedChannels = currentCombinedChannels;
            s.selectedChannels        = selectedChannels;
            s.customChannelCount      = customChannelCount;
            s.undoStack               = undoStack;
            s.activeChannels          = activeChannels;
            s.lastActiveIdx           = lastActiveIdx;
            s.sectionChecks = {
                inputs:  document.getElementById('includeInputs').checked,
                aux:     document.getElementById('includeAux').checked,
                groups:  document.getElementById('includeGroups').checked,
                matrix:  document.getElementById('includeMatrix').checked,
            };
        }

        function switchTab(idx) {
            saveTabState();
            activeTab = idx;
            loadTabState();
            renderTabs();
            // Restore UI state for this tab
            const s = getTab();
            const hasChannels = s.currentCombinedChannels.length > 0;
            document.getElementById('previewArea').style.display = hasChannels ? 'block' : 'none';
            document.getElementById('message').style.display = 'none';
            document.getElementById('fileInput').value = '';
            if (hasChannels) {
                showPreview(s.currentCombinedChannels);
                refreshSectionCounts();
            }
            updateUndoBtn();
            updateBulkBar();
        }

        function addTab() {
            saveTabState();
            const n = sessions.length + 1;
            sessions.push(newSessionState('Session ' + n));
            activeTab = sessions.length - 1;
            loadTabState();
            renderTabs();
            // Clear the UI for the fresh tab
            document.getElementById('previewArea').style.display = 'none';
            document.getElementById('message').style.display = 'none';
            document.getElementById('fileInput').value = '';
            updateUndoBtn();
            updateBulkBar();
        }

        function closeTab(idx) {
            if (sessions.length === 1) return; // keep at least one
            sessions.splice(idx, 1);
            if (activeTab >= sessions.length) activeTab = sessions.length - 1;
            loadTabState();
            renderTabs();
            const s = getTab();
            const hasChannels = s.currentCombinedChannels.length > 0;
            document.getElementById('previewArea').style.display = hasChannels ? 'block' : 'none';
            if (hasChannels) { showPreview(s.currentCombinedChannels); refreshSectionCounts(); }
            updateUndoBtn();
            updateBulkBar();
        }

        function renderTabs() {
            const bar = document.getElementById('tabBar');
            bar.innerHTML = '';
            sessions.forEach((s, i) => {
                const tab = document.createElement('div');
                tab.className = 'tab' + (i === activeTab ? ' active' : '');

                function startTabRename(tabEl) {
                    const existingInput = tabEl.querySelector('input');
                    if (existingInput) return;
                    const span = tabEl.querySelector('.tab-name');
                    if (!span) return;
                    const input = document.createElement('input');
                    input.value = sessions[i].name;
                    input.style.cssText = 'width:' + Math.max(60, sessions[i].name.length * 9) + 'px;font:inherit;border:none;outline:1px solid #007aff;border-radius:3px;padding:0 3px;background:transparent;color:inherit;';
                    tabEl.replaceChild(input, span);
                    input.focus();
                    input.select();
                    let committed = false;
                    function commit() {
                        if (committed) return;
                        committed = true;
                        const val = input.value.trim();
                        sessions[i].name = val || sessions[i].name;
                        tabEl.replaceChild(span, input);
                        span.textContent = sessions[i].name;
                    }
                    input.addEventListener('blur', commit);
                    input.addEventListener('keydown', (ev) => {
                        if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
                        if (ev.key === 'Escape') { committed = true; tabEl.replaceChild(span, input); span.textContent = sessions[i].name; }
                        ev.stopPropagation();
                    });
                }

                const nameSpan = document.createElement('span');
                nameSpan.className = 'tab-name';
                nameSpan.textContent = s.name;
                nameSpan.title = 'Double-click to rename';

                let clickTimer = null;
                tab.addEventListener('click', (e) => {
                    if (e.target.tagName === 'INPUT' || e.target.classList.contains('tab-close')) return;
                    if (clickTimer) {
                        // Double-click detected
                        clearTimeout(clickTimer);
                        clickTimer = null;
                        if (i !== activeTab) { switchTab(i); setTimeout(() => startTabRename(document.querySelectorAll('.tab')[i]), 50); }
                        else startTabRename(tab);
                    } else {
                        clickTimer = setTimeout(() => { clickTimer = null; if (i !== activeTab) switchTab(i); }, 220);
                    }
                });

                tab.appendChild(nameSpan);

                if (sessions.length > 1) {
                    const close = document.createElement('span');
                    close.className = 'tab-close';
                    close.textContent = '×';
                    close.title = 'Close session';
                    close.onclick = (e) => { e.stopPropagation(); closeTab(i); };
                    tab.appendChild(close);
                }

                bar.appendChild(tab);
            });

            const addBtn = document.createElement('button');
            addBtn.className = 'tab-add';
            addBtn.textContent = '+';
            addBtn.title = 'New session';
            addBtn.onclick = addTab;
            bar.appendChild(addBtn);
        }

        // Initialise
        loadTabState();
        renderTabs();

        // ── Non-session globals ──────────────────────────────────────────────
        let draggedIndex = null;
        let pendingDownloadType = 'template';

        function saveUndo() {
            undoStack.push({
                channels: currentCombinedChannels.map(ch => ({ ...ch })),
                selected: new Set(selectedChannels),
                customCount: customChannelCount
            });
            if (undoStack.length > 50) undoStack.shift();
            // updateUndoBtn may not exist yet on first call — defer safely
            setTimeout(updateUndoBtn, 0);
        }

        function undo() {
            if (undoStack.length === 0) return;
            const prev = undoStack.pop();
            currentCombinedChannels.length = 0;
            currentCombinedChannels.push(...prev.channels);
            selectedChannels = prev.selected;
            customChannelCount = prev.customCount;
            activeChannels.clear();
            showPreview(currentCombinedChannels);
            refreshSectionCounts();
            updateBulkBar();
            updateUndoBtn();
        }

        function updateUndoBtn() {
            const btn = document.getElementById('undoBtn');
            if (!btn) return;
            btn.disabled = undoStack.length === 0;
            btn.style.opacity = undoStack.length === 0 ? '0.4' : '1';
        }
        let stereoMode = 'split';
        let didDrag = false;
        
        // Drag and drop
        const uploadArea = document.getElementById('uploadArea');
        
        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.classList.add('dragover');
        });
        
        uploadArea.addEventListener('dragleave', () => {
            uploadArea.classList.remove('dragover');
        });
        
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.classList.remove('dragover');
            const file = e.dataTransfer.files[0];
            const name = file ? file.name.toLowerCase() : '';
            if (file && (name.endsWith('.rtf') || name.endsWith('.rivagepm') || name.endsWith('.tar.gz') || name.endsWith('.scn') || name.endsWith('.snap') || name.endsWith('.dsh') || name.endsWith('.dm7f') || name.endsWith('.dat') || name.endsWith('.emo'))) {
                handleFile(file);
            } else {
                showMessage('Please upload a .rtf (DiGiCo), .RIVAGEPM (Yamaha Rivage), .tar.gz (A&H dLive/Avantis), .dat (A&H SQ), .scn (X32/M32), .snap (Wing), .dsh (Avid S6L), .dm7f (Yamaha DM7), or .emo (Waves LV1) file', 'error');
            }
        });
        
        function handleFile(file) {
            if (!file) return;

            ['inputs', 'aux', 'groups', 'matrix'].forEach(type => clearColorFromSection(type));

            const formData = new FormData();
            formData.append('file', file);
            
            showMessage('Processing file...', 'success');
            
            fetch('/convert', {
                method: 'POST',
                body: formData
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    // Store all sections
                    parsedSections = data.sections;
                    
                    // Update section counts
                    document.getElementById('inputsCount').textContent = data.counts.inputs;
                    document.getElementById('auxCount').textContent = data.counts.aux;
                    document.getElementById('groupsCount').textContent = data.counts.groups;
                    document.getElementById('matrixCount').textContent = data.counts.matrix;
                    
                    // Show/hide checkboxes based on what's available
                    document.getElementById('includeInputs').disabled = data.counts.inputs === 0;
                    document.getElementById('includeAux').disabled = data.counts.aux === 0;
                    document.getElementById('includeGroups').disabled = data.counts.groups === 0;
                    document.getElementById('includeMatrix').disabled = data.counts.matrix === 0;
                    
                    // Update preview with selected sections
                    updateSectionPreview();

                    const total = data.counts.inputs + data.counts.aux + data.counts.groups + data.counts.matrix;
                    if (total === 0) {
                        const ext = name.split('.').pop().toLowerCase();
                        const hint = (ext === 'rtf')
                            ? ' Please ensure Include: Channels is selected when saving the Session Report.'
                            : (ext === 'show')
                            ? ' If using a Wing show file, make sure at least one snapshot has been saved into it.'
                            : '';
                        showMessage('✗ "' + file.name + '" — No Channels Found.' + hint, 'error');
                    } else {
                        showMessage(`✓ "${file.name}" — ${total} total channels (${data.counts.inputs} inputs, ${data.counts.aux} aux, ${data.counts.groups} groups, ${data.counts.matrix} matrix)`, 'success');
                    }
                } else {
                    showMessage('✗ Error: ' + data.error, 'error');
                }
            })
            .catch(err => {
                showMessage('✗ Error processing file: ' + err, 'error');
            });
        }
        
        function updateSectionPreview() {
            // ALWAYS show all channels from all sections
            let allChannels = [].concat(
                parsedSections.inputs,
                parsedSections.aux,
                parsedSections.groups,
                parsedSections.matrix
            );
            
            // Store combined channels
            currentCombinedChannels = allChannels;
            
            // Auto-select only channels from CHECKED sections
            selectedChannels.clear();
            
            let currentIndex = 0;
            
            // Select inputs if checked
            if (document.getElementById('includeInputs').checked) {
                for (let i = 0; i < parsedSections.inputs.length; i++) {
                    selectedChannels.add(currentIndex + i);
                }
            }
            currentIndex += parsedSections.inputs.length;
            
            // Select aux if checked
            if (document.getElementById('includeAux').checked) {
                for (let i = 0; i < parsedSections.aux.length; i++) {
                    selectedChannels.add(currentIndex + i);
                }
            }
            currentIndex += parsedSections.aux.length;
            
            // Select groups if checked
            if (document.getElementById('includeGroups').checked) {
                for (let i = 0; i < parsedSections.groups.length; i++) {
                    selectedChannels.add(currentIndex + i);
                }
            }
            currentIndex += parsedSections.groups.length;
            
            // Select matrix if checked
            if (document.getElementById('includeMatrix').checked) {
                for (let i = 0; i < parsedSections.matrix.length; i++) {
                    selectedChannels.add(currentIndex + i);
                }
            }
            
            showPreview(allChannels);
        }
        
        function showPreview(channels) {
            const previewArea = document.getElementById('previewArea');
            const previewList = document.getElementById('previewList');
            const trackCount = document.getElementById('trackCount');
            
            previewList.innerHTML = '';
            
            channels.forEach((ch, idx) => {
                const div = document.createElement('div');
                div.className = 'track-item' + (activeChannels.has(idx) ? ' active-highlight' : '');
                div.draggable = true;
                div.dataset.index = idx;

                // Drag events
                div.addEventListener('dragstart', handleDragStart);
                div.addEventListener('dragover', handleDragOver);
                div.addEventListener('drop', handleDrop);
                div.addEventListener('dragend', handleDragEnd);

                // Row click for multi-select (ignore interactive children)
                div.addEventListener('click', (e) => {
                    if (didDrag) return;
                    if (e.target.closest('input, button, .track-color-btn, .drag-handle')) return;
                    handleRowClick(e, idx);
                });
                
                // Drag handle
                const dragHandle = document.createElement('span');
                dragHandle.className = 'drag-handle';
                dragHandle.textContent = '☰';
                
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.className = 'track-checkbox';
                checkbox.dataset.idx = idx;
                checkbox.checked = selectedChannels.has(idx);
                checkbox.onchange = () => {
                    if (activeChannels.has(idx) && activeChannels.size > 1) {
                        if (checkbox.checked) {
                            activeChannels.forEach(i => selectedChannels.add(i));
                        } else {
                            activeChannels.forEach(i => selectedChannels.delete(i));
                        }
                        showPreview(currentCombinedChannels);
                    } else {
                        toggleChannel(idx);
                    }
                };
                
                const number = document.createElement('span');
                number.className = 'track-number';
                number.textContent = ch.number;
                
                // Add type badge
                const badge = document.createElement('span');
                badge.className = 'track-badge';
                
                // Determine badge text and color based on type
                const badgeInfo = {
                    'inputs': { text: 'IN', class: 'badge-inputs' },
                    'aux': { text: 'AUX', class: 'badge-aux' },
                    'groups': { text: 'GRP', class: 'badge-groups' },
                    'matrix': { text: 'MTX', class: 'badge-matrix' },
                    'custom': { text: 'CUST', class: 'badge-custom' }
                };
                
                const info = badgeInfo[ch.type] || { text: 'CH', class: 'badge-inputs' };
                badge.textContent = info.text;
                badge.classList.add(info.class);
                
                const name = document.createElement('span');
                name.className = 'track-name';
                name.textContent = ch.name;
                
                name.addEventListener('dblclick', (e) => { e.stopPropagation(); startEditName(idx, name, ch); });
                name.title = 'Double-click to rename';

                // Color swatch
                const colorBtn = document.createElement('div');
                colorBtn.className = 'track-color-btn' + (ch.color ? ' has-color' : '');
                if (ch.color) colorBtn.style.backgroundColor = ch.color;
                colorBtn.title = 'Click to set track color';

                const colorInput = document.createElement('input');
                colorInput.type = 'color';
                colorInput.value = ch.color || '#ff6b6b';
                colorBtn.appendChild(colorInput);

                // Clear badge — appears on hover when color is set
                const clearBadge = document.createElement('span');
                clearBadge.className = 'color-clear-badge';
                clearBadge.textContent = '✕';
                clearBadge.title = 'Clear color';
                colorBtn.appendChild(clearBadge);

                colorBtn.addEventListener('click', (e) => { e.stopPropagation(); colorInput.click(); });

                clearBadge.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const targets = (activeChannels.has(idx) && activeChannels.size > 1)
                        ? Array.from(activeChannels) : [idx];
                    targets.forEach(i => { currentCombinedChannels[i].color = null; });
                    if (targets.length > 1) {
                        showPreview(currentCombinedChannels);
                    } else {
                        colorBtn.style.backgroundColor = '';
                        colorBtn.classList.remove('has-color');
                        colorInput.value = '#ff6b6b';
                    }
                });

                colorInput.addEventListener('change', (e) => {
                    const targets = (activeChannels.has(idx) && activeChannels.size > 1)
                        ? Array.from(activeChannels) : [idx];
                    targets.forEach(i => { currentCombinedChannels[i].color = e.target.value; });
                    if (targets.length > 1) {
                        showPreview(currentCombinedChannels);
                    } else {
                        colorBtn.style.backgroundColor = e.target.value;
                        colorBtn.classList.add('has-color');
                    }
                });

                const editBtn = document.createElement('button');
                editBtn.className = 'track-edit';
                editBtn.textContent = '✎';
                editBtn.title = 'Rename channel';
                editBtn.onclick = (e) => { e.stopPropagation(); startEditName(idx, name, ch); };

                // Stereo toggle button
                const stereoBtn = document.createElement('button');
                const isStereo = ch.number.endsWith('s');
                stereoBtn.className = 'stereo-btn' + (isStereo ? ' is-stereo' : '');
                stereoBtn.textContent = isStereo ? 'Stereo' : 'Mono';
                stereoBtn.title = isStereo ? 'Click to set mono' : 'Click to set stereo';
                stereoBtn.onclick = (e) => {
                    e.stopPropagation();
                    const targets = (activeChannels.has(idx) && activeChannels.size > 1)
                        ? Array.from(activeChannels) : [idx];
                    saveUndo();
                    targets.forEach(i => {
                        const c = currentCombinedChannels[i];
                        if (c.number.endsWith('s')) {
                            c.number = c.number.slice(0, -1);
                        } else {
                            c.number = c.number + 's';
                        }
                    });
                    showPreview(currentCombinedChannels);
                };

                div.appendChild(dragHandle);
                div.appendChild(checkbox);
                div.appendChild(number);
                div.appendChild(badge);
                div.appendChild(stereoBtn);
                div.appendChild(colorBtn);
                div.appendChild(name);
                div.appendChild(editBtn);

                const deleteBtn = document.createElement('button');
                deleteBtn.className = 'track-delete';
                deleteBtn.textContent = '×';
                deleteBtn.title = 'Remove channel';
                deleteBtn.onclick = (e) => { e.stopPropagation(); saveUndo(); deleteChannel(idx); };
                div.appendChild(deleteBtn);

                previewList.appendChild(div);
            });
            
            trackCount.textContent = channels.length;
            updateSelectedCount();
            previewArea.style.display = 'block';

            // Click on list background clears active selection
            previewList.addEventListener('click', (e) => {
                if (!e.target.closest('.track-item')) {
                    clearActiveSelection();
                }
            }, { once: true });

            // Wire up bulk color swatch after render
            const bulkSwatch = document.getElementById('bulkColorSwatch');
            const bulkInput = document.getElementById('bulkColorInput');
            bulkSwatch.onclick = () => bulkInput.click();
            bulkInput.onchange = (e) => {
                activeChannels.forEach(i => { currentCombinedChannels[i].color = e.target.value; });
                showPreview(currentCombinedChannels);
                updateBulkBar();
            };
        }
        
        let scrollInterval = null;
        let dropInsertBefore = true; // whether to insert before or after target

        function clearDropIndicators() {
            document.querySelectorAll('.drop-above, .drop-below').forEach(el => {
                el.classList.remove('drop-above', 'drop-below');
            });
        }

        function handleDragStart(e) {
            didDrag = true;
            draggedIndex = parseInt(e.currentTarget.dataset.index);
            if (!activeChannels.has(draggedIndex)) {
                activeChannels.clear();
            }
            e.dataTransfer.effectAllowed = 'move';

            // Dim entire block in place
            document.querySelectorAll('.track-item').forEach(el => {
                if (activeChannels.has(parseInt(el.dataset.index))) {
                    el.classList.add('dragging');
                }
            });
            if (!activeChannels.has(draggedIndex)) {
                e.currentTarget.classList.add('dragging');
            }

            // Build a ghost showing all active rows
            const ghost = document.createElement('div');
            ghost.style.cssText = 'position:fixed;top:-9999px;left:-9999px;pointer-events:none;z-index:9999;';
            const indices = activeChannels.size > 0 ? Array.from(activeChannels).sort((a,b)=>a-b) : [draggedIndex];
            indices.forEach(i => {
                const src = document.querySelector(`.track-item[data-index="${i}"]`);
                if (src) {
                    const clone = src.cloneNode(true);
                    clone.style.cssText = 'opacity:1;width:' + src.offsetWidth + 'px;margin:0;';
                    clone.classList.remove('dragging');
                    ghost.appendChild(clone);
                }
            });
            document.body.appendChild(ghost);
            e.dataTransfer.setDragImage(ghost, e.offsetX, 20);
            setTimeout(() => ghost.remove(), 0);
        }

        function handleDragOver(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';

            const target = e.target.closest('.track-item');
            clearDropIndicators();
            if (target && target.dataset.index !== undefined) {
                const rect = target.getBoundingClientRect();
                const midY = rect.top + rect.height / 2;
                dropInsertBefore = e.clientY < midY;
                target.classList.add(dropInsertBefore ? 'drop-above' : 'drop-below');
            }

            // Auto-scroll the preview list
            const list = document.getElementById('previewList');
            const listRect = list.getBoundingClientRect();
            const scrollZone = 50;
            if (scrollInterval) { clearInterval(scrollInterval); scrollInterval = null; }
            if (e.clientY < listRect.top + scrollZone) {
                scrollInterval = setInterval(() => { list.scrollTop -= 8; }, 16);
            } else if (e.clientY > listRect.bottom - scrollZone) {
                scrollInterval = setInterval(() => { list.scrollTop += 8; }, 16);
            }

            return false;
        }

        function handleDrop(e) {
            if (e.stopPropagation) e.stopPropagation();
            clearDropIndicators();
            if (scrollInterval) { clearInterval(scrollInterval); scrollInterval = null; }

            const target = e.target.closest('.track-item');
            if (!target || target.dataset.index === undefined) return false;

            let dropIndex = parseInt(target.dataset.index);
            // Adjust insert position based on whether we're dropping above/below
            if (!dropInsertBefore) dropIndex = Math.min(dropIndex + 1, currentCombinedChannels.length - 1);
            saveUndo();
            const dropItem = currentCombinedChannels[dropIndex];

            // Snapshot selection by object reference before reorder
            const selectedObjects = new Set(Array.from(selectedChannels).map(i => currentCombinedChannels[i]));
            const activeObjects = new Set(Array.from(activeChannels).map(i => currentCombinedChannels[i]));

            const isMulti = activeChannels.size > 1 && activeChannels.has(draggedIndex);

            if (isMulti) {
                // Multi-drag: move all active channels together
                const dragging = Array.from(activeObjects);
                const draggingSet = activeObjects;

                if (!draggingSet.has(dropItem)) {
                    const kept = currentCombinedChannels.filter(ch => !draggingSet.has(ch));
                    const insertAt = kept.indexOf(dropItem);
                    currentCombinedChannels.length = 0;
                    if (insertAt === -1) {
                        currentCombinedChannels.push(...kept, ...dragging);
                    } else {
                        currentCombinedChannels.push(...kept.slice(0, insertAt), ...dragging, ...kept.slice(insertAt));
                    }
                }
            } else if (draggedIndex !== dropIndex) {
                // Single drag
                const draggedItem = currentCombinedChannels[draggedIndex];
                currentCombinedChannels.splice(draggedIndex, 1);
                currentCombinedChannels.splice(dropIndex, 0, draggedItem);
            }

            // Rebuild index sets from object references
            selectedChannels.clear();
            activeChannels.clear();
            currentCombinedChannels.forEach((ch, i) => {
                if (selectedObjects.has(ch)) selectedChannels.add(i);
                if (activeObjects.has(ch)) activeChannels.add(i);
            });

            showPreview(currentCombinedChannels);
            updateBulkBar();
            return false;
        }
        
        function handleDragEnd(e) {
            document.querySelectorAll('.track-item.dragging').forEach(el => el.classList.remove('dragging'));
            clearDropIndicators();
            if (scrollInterval) { clearInterval(scrollInterval); scrollInterval = null; }
            draggedIndex = null;
            setTimeout(() => { didDrag = false; }, 0);
        }
        
        function toggleChannel(idx) {
            if (selectedChannels.has(idx)) {
                selectedChannels.delete(idx);
            } else {
                selectedChannels.add(idx);
            }
            updateSelectedCount();
        }
        
        function updateSelectedCount() {
            document.getElementById('selectedCount').textContent = selectedChannels.size;
        }
        
        function selectAll() {
            selectedChannels.clear();
            currentCombinedChannels.forEach((ch, idx) => {
                selectedChannels.add(idx);
            });

            document.querySelectorAll('.track-checkbox').forEach(cb => { cb.checked = true; });

            // Sync section checkboxes
            ['includeInputs', 'includeAux', 'includeGroups', 'includeMatrix'].forEach(id => {
                document.getElementById(id).checked = true;
            });

            updateSelectedCount();
        }

        function selectNone() {
            selectedChannels.clear();

            document.querySelectorAll('.track-checkbox').forEach(cb => { cb.checked = false; });

            // Sync section checkboxes
            ['includeInputs', 'includeAux', 'includeGroups', 'includeMatrix'].forEach(id => {
                document.getElementById(id).checked = false;
            });

            updateSelectedCount();
        }

        function removeUnnamed() {
            const toRemove = new Set();
            currentCombinedChannels.forEach((ch, idx) => {
                if (ch.is_default) toRemove.add(idx);
            });
            if (toRemove.size === 0) return;

            saveUndo();

            // Remap selectedChannels and activeChannels around removed indices
            const newSelected = new Set();
            selectedChannels.forEach(i => {
                if (toRemove.has(i)) return;
                let shift = 0;
                toRemove.forEach(r => { if (r < i) shift++; });
                newSelected.add(i - shift);
            });
            selectedChannels = newSelected;

            const newActive = new Set();
            activeChannels.forEach(i => {
                if (toRemove.has(i)) return;
                let shift = 0;
                toRemove.forEach(r => { if (r < i) shift++; });
                newActive.add(i - shift);
            });
            activeChannels = newActive;

            const kept = currentCombinedChannels.filter((_, idx) => !toRemove.has(idx));
            currentCombinedChannels.length = 0;
            currentCombinedChannels.push(...kept);

            showPreview(currentCombinedChannels);
            refreshSectionCounts();
            updateBulkBar();
        }

        function downloadTemplate() {
            if (selectedChannels.size === 0) {
                showMessage('Please select at least one track', 'error');
                return;
            }

            pendingDownloadType = 'template';
            document.getElementById('filenameModal').querySelector('h2').textContent = 'Save Track Template';
            // Show filename modal
            document.getElementById('filenameModal').classList.add('active');
            document.getElementById('filenameInput').focus();
            document.getElementById('filenameInput').select();
        }

        function downloadCSV() {
            if (selectedChannels.size === 0) {
                showMessage('Please select at least one track', 'error');
                return;
            }

            pendingDownloadType = 'csv';
            document.getElementById('filenameModal').querySelector('h2').textContent = 'Export as CSV';
            document.getElementById('filenameModal').classList.add('active');
            document.getElementById('filenameInput').focus();
            document.getElementById('filenameInput').select();
        }
        
        function closeFilenameModal() {
            document.getElementById('filenameModal').classList.remove('active');
        }
        
        function confirmDownload() {
            const filename = document.getElementById('filenameInput').value.trim();

            if (!filename) {
                alert('Please enter a filename');
                return;
            }

            closeFilenameModal();

            // Get selected channels from current combined list
            const selected = [];
            currentCombinedChannels.forEach((ch, idx) => {
                if (selectedChannels.has(idx)) {
                    selected.push(ch);
                }
            });

            if (pendingDownloadType === 'csv') {
                // Build CSV in browser
                const typeLabels = { inputs: 'Input', aux: 'Aux', groups: 'Group', matrix: 'Matrix' };
                const rows = [['Type', 'Number', 'Name']];
                selected.forEach(ch => {
                    const type = typeLabels[ch.type] || ch.type;
                    const name = ch.name.includes(',') ? '"' + ch.name + '"' : ch.name;
                    rows.push([type, ch.number, name]);
                });
                const csvContent = rows.map(r => r.join(',')).join('\\n');
                const blob = new Blob([csvContent], { type: 'text/csv' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename + '.csv';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                showMessage(`✓ CSV "${filename}.csv" with ${selected.length} channels exported!`, 'success');
                return;
            }

            // Request template generation
            fetch('/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ channels: selected, stereo_mode: stereoMode })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    // Create blob and download
                    const blob = new Blob([data.template], { type: 'text/plain' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = filename + '.RTrackTemplate';
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);

                    showMessage(`✓ Template "${filename}.RTrackTemplate" with ${selected.length} tracks downloaded!`, 'success');
                } else {
                    showMessage('✗ Error generating template', 'error');
                }
            })
            .catch(err => {
                showMessage('✗ Error: ' + err, 'error');
            });
        }
        
        function setStereoMode(mode) {
            stereoMode = mode;
            ['optSplit', 'optSplitBottom'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.classList.toggle('active', mode === 'split');
            });
            ['optStereo', 'optStereoBottom'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.classList.toggle('active', mode === 'stereo');
            });
        }

        // Keyboard shortcuts on track list
        document.addEventListener('keydown', (e) => {
            if (document.activeElement && document.activeElement.tagName === 'INPUT') return;
            if (document.activeElement && document.activeElement.tagName === 'TEXTAREA') return;

            // Cmd/Ctrl+A — select all
            if ((e.metaKey || e.ctrlKey) && e.key === 'a') {
                if (currentCombinedChannels.length === 0) return;
                e.preventDefault();
                activeChannels.clear();
                currentCombinedChannels.forEach((_, i) => activeChannels.add(i));
                lastActiveIdx = currentCombinedChannels.length - 1;
                updateBulkBar();
                document.querySelectorAll('.track-item').forEach(el => el.classList.add('active-highlight'));
            }

            // Delete / Backspace — remove highlighted channels
            if ((e.key === 'Delete' || e.key === 'Backspace') && activeChannels.size > 0) {
                e.preventDefault();
                deleteSelectedChannels();
            }

            // Cmd/Ctrl+Z — undo
            if ((e.metaKey || e.ctrlKey) && e.key === 'z') {
                e.preventDefault();
                undo();
            }
        });

        // Allow Enter key to confirm in modals
        document.getElementById('filenameInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                confirmDownload();
            }
        });

        document.getElementById('newChannelName').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                addCustomChannel();
            }
        });
        
        function applyColorToSection(type, color) {
            const idMap = { inputs: 'colorInputs', aux: 'colorAux', groups: 'colorGroups', matrix: 'colorMatrix' };
            const swatchIdMap = { inputs: 'swatchInputs', aux: 'swatchAux', groups: 'swatchGroups', matrix: 'swatchMatrix' };
            const swatch = document.getElementById(swatchIdMap[type]);
            if (swatch) {
                swatch.style.backgroundColor = color;
                swatch.classList.add('has-color');
            }
            currentCombinedChannels.forEach(ch => {
                if (ch.type === type) ch.color = color;
            });
            showPreview(currentCombinedChannels);
        }

        function clearColorFromSection(type) {
            const swatchIdMap = { inputs: 'swatchInputs', aux: 'swatchAux', groups: 'swatchGroups', matrix: 'swatchMatrix' };
            const colorIdMap = { inputs: 'colorInputs', aux: 'colorAux', groups: 'colorGroups', matrix: 'colorMatrix' };
            const swatch = document.getElementById(swatchIdMap[type]);
            if (swatch) {
                swatch.style.backgroundColor = '';
                swatch.classList.remove('has-color');
                document.getElementById(colorIdMap[type]).value = '#ff6b6b';
            }
            currentCombinedChannels.forEach(ch => {
                if (ch.type === type) ch.color = null;
            });
            showPreview(currentCombinedChannels);
        }

        function handleRowClick(e, idx) {
            if (e.metaKey || e.ctrlKey) {
                // Toggle individual
                if (activeChannels.has(idx)) activeChannels.delete(idx);
                else activeChannels.add(idx);
                lastActiveIdx = idx;
            } else if (e.shiftKey && lastActiveIdx !== null) {
                // Range select
                const start = Math.min(lastActiveIdx, idx);
                const end = Math.max(lastActiveIdx, idx);
                for (let i = start; i <= end; i++) activeChannels.add(i);
            } else {
                // Single select (clear others)
                activeChannels.clear();
                activeChannels.add(idx);
                lastActiveIdx = idx;
            }
            updateBulkBar();
            // Refresh just the highlight classes without full re-render
            document.querySelectorAll('.track-item').forEach((el, i) => {
                el.classList.toggle('active-highlight', activeChannels.has(parseInt(el.dataset.index)));
            });
        }

        function updateBulkBar() {
            const bar = document.getElementById('bulkBar');
            const count = document.getElementById('bulkCount');
            if (activeChannels.size > 1) {
                bar.classList.add('visible');
                count.textContent = activeChannels.size + ' channel' + (activeChannels.size > 1 ? 's' : '') + ' selected';
            } else {
                bar.classList.remove('visible');
            }
        }

        function clearActiveSelection() {
            activeChannels.clear();
            lastActiveIdx = null;
            updateBulkBar();
            document.querySelectorAll('.track-item').forEach(el => el.classList.remove('active-highlight'));
        }

        function clearBulkColors() {
            activeChannels.forEach(i => { currentCombinedChannels[i].color = null; });
            showPreview(currentCombinedChannels);
            updateBulkBar();
        }

        function refreshSectionCounts() {
            const counts = { inputs: 0, aux: 0, groups: 0, matrix: 0 };
            currentCombinedChannels.forEach(ch => {
                if (counts[ch.type] !== undefined) counts[ch.type]++;
            });
            document.getElementById('inputsCount').textContent = counts.inputs;
            document.getElementById('auxCount').textContent = counts.aux;
            document.getElementById('groupsCount').textContent = counts.groups;
            document.getElementById('matrixCount').textContent = counts.matrix;
        }

        function startEditName(idx, nameSpan, ch) {
            const input = document.createElement('input');
            input.type = 'text';
            input.value = ch.name;
            input.style.cssText = 'flex: 1; padding: 2px 6px; border: 1px solid #007aff; border-radius: 4px; font-size: 13px; font-family: inherit; outline: none;';

            let saved = false;
            function save() {
                if (saved) return;
                saved = true;
                const newName = input.value.trim();
                if (newName && newName !== ch.name) {
                    saveUndo();
                    currentCombinedChannels[idx].name = newName;
                }
                showPreview(currentCombinedChannels);
            }

            input.addEventListener('blur', save);
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
                if (e.key === 'Escape') { saved = true; showPreview(currentCombinedChannels); }
                if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                    e.preventDefault();
                    const newName = input.value.trim();
                    if (newName) currentCombinedChannels[idx].name = newName;
                    saved = true;
                    const nextIdx = e.key === 'ArrowDown' ? idx + 1 : idx - 1;
                    if (nextIdx >= 0 && nextIdx < currentCombinedChannels.length) {
                        showPreview(currentCombinedChannels);
                        // After re-render, find the next name span and start editing it
                        const items = document.querySelectorAll('.track-item');
                        const nextItem = items[nextIdx];
                        if (nextItem) {
                            const nextName = nextItem.querySelector('.track-name');
                            if (nextName) startEditName(nextIdx, nextName, currentCombinedChannels[nextIdx]);
                        }
                    } else {
                        showPreview(currentCombinedChannels);
                    }
                }
            });

            nameSpan.replaceWith(input);
            input.focus();
            input.select();
        }

        function openAddChannelModal() {
            document.getElementById('addChannelModal').classList.add('active');
            document.getElementById('newChannelName').focus();
        }

        function closeAddChannelModal() {
            document.getElementById('addChannelModal').classList.remove('active');
            document.getElementById('newChannelName').value = '';
            document.getElementById('newChannelStereo').checked = false;
            document.getElementById('newChannelType').value = 'inputs';
            document.getElementById('newChannelQty').value = '1';
        }

        function addCustomChannel() {
            const baseName = document.getElementById('newChannelName').value.trim();
            if (!baseName) {
                alert('Please enter a channel name');
                return;
            }

            const type = document.getElementById('newChannelType').value;
            const isStereo = document.getElementById('newChannelStereo').checked;
            const qty = Math.max(1, parseInt(document.getElementById('newChannelQty').value) || 1);

            saveUndo();
            for (let i = 0; i < qty; i++) {
                customChannelCount++;
                const name = qty > 1 ? `${baseName} ${i + 1}` : baseName;
                const number = isStereo ? 'C' + customChannelCount + 's' : 'C' + customChannelCount;
                currentCombinedChannels.push({ number, name, type, isCustom: true });
                selectedChannels.add(currentCombinedChannels.length - 1);
            }

            closeAddChannelModal();
            showPreview(currentCombinedChannels);
            refreshSectionCounts();
            document.getElementById('previewArea').style.display = 'block';
        }

        function deleteChannelWithConfirm(idx) {
            const ch = currentCombinedChannels[idx];
            if (!confirm(`Remove "${ch.name}"?`)) return;
            saveUndo();
            deleteChannel(idx);
        }

        function deleteSelectedChannels() {
            if (activeChannels.size === 0) return;
            const names = Array.from(activeChannels).map(i => currentCombinedChannels[i].name);
            const label = names.length === 1
                ? `Remove "${names[0]}"?`
                : `Remove ${names.length} selected channels?`;
            if (!confirm(label)) return;
            saveUndo();
            // Delete highest indices first to avoid shifting
            const indices = Array.from(activeChannels).sort((a, b) => b - a);
            indices.forEach(i => deleteChannel(i));
        }

        function deleteChannel(idx) {
            const newSelected = new Set();
            selectedChannels.forEach(i => {
                if (i < idx) newSelected.add(i);
                else if (i > idx) newSelected.add(i - 1);
            });
            selectedChannels = newSelected;
            const newActive = new Set();
            activeChannels.forEach(i => {
                if (i < idx) newActive.add(i);
                else if (i > idx) newActive.add(i - 1);
            });
            activeChannels = newActive;
            currentCombinedChannels.splice(idx, 1);
            showPreview(currentCombinedChannels);
            refreshSectionCounts();
            updateBulkBar();
        }

        function reset() {
            const hasChannels = currentCombinedChannels.length > 0;
            if (hasChannels && !confirm('Replace the current session with a new file? This will clear all channels in this tab.')) return;
            document.getElementById('fileInput').value = '';
            document.getElementById('previewArea').style.display = 'none';
            document.getElementById('message').style.display = 'none';
            const fresh = newSessionState(getTab().name);
            sessions[activeTab] = fresh;
            loadTabState();
            updateBulkBar();
            updateUndoBtn();
            // Open file picker immediately
            document.getElementById('fileInput').click();
        }
        
        function showMessage(msg, type) {
            const msgDiv = document.getElementById('message');
            msgDiv.textContent = msg;
            msgDiv.className = 'message ' + type;
            msgDiv.style.display = '';
        }
        
        // Heartbeat check to detect server disconnect
        let heartbeatInterval;
        let missedHeartbeats = 0;
        
        function checkHeartbeat() {
            fetch('/heartbeat', { 
                method: 'GET',
                cache: 'no-cache'
            })
            .then(response => {
                if (response.ok) {
                    missedHeartbeats = 0;
                } else {
                    missedHeartbeats++;
                }
            })
            .catch(err => {
                missedHeartbeats++;
                if (missedHeartbeats >= 2) {
                    // Server is down
                    clearInterval(heartbeatInterval);
                    document.getElementById('disconnectOverlay').style.display = 'flex';
                }
            });
        }
        
        // Check every 3 seconds
        heartbeatInterval = setInterval(checkHeartbeat, 3000);
    </script>
</body>
</html>
        '''
        
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode())
    
    def handle_conversion(self):
        """Handle file upload and parsing"""
        try:
            # Parse multipart form data
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)

            # Simple multipart parsing (for single file upload)
            boundary = self.headers['Content-Type'].split('boundary=')[1]
            parts = body.split(('--' + boundary).encode())

            file_content = None
            filename = ''
            for part in parts:
                if b'filename=' in part:
                    # Extract filename
                    fn_match = re.search(rb'filename="([^"]+)"', part)
                    if fn_match:
                        filename = fn_match.group(1).decode('utf-8', errors='replace').lower()
                    file_start = part.find(b'\r\n\r\n') + 4
                    file_end = part.rfind(b'\r\n')
                    file_content = part[file_start:file_end]
                    break

            if not file_content:
                self.send_json({'success': False, 'error': 'No file found in upload'})
                return

            # Dispatch to the correct parser based on file extension
            if filename.endswith('.rivagepm'):
                parsed_data = parse_rivage_pm_show_file(file_content)
            elif filename.endswith('.tar.gz'):
                parsed_data = parse_dlive_show_file(file_content)
            elif filename.endswith('.scn'):
                parsed_data = parse_m32_show_file(file_content)
            elif filename.endswith('.snap'):
                parsed_data = parse_wing_show_file(file_content)
            elif filename.endswith('.dsh'):
                parsed_data = parse_s6l_show_file(file_content)
            elif filename.endswith('.dm7f'):
                parsed_data = parse_dm7_show_file(file_content)
            elif filename.lower().endswith('.dat'):
                parsed_data = parse_sq_show_file(file_content)
            elif filename.lower().endswith('.emo'):
                parsed_data = parse_lv1_show_file(file_content)
            else:
                parsed_data = parse_digico_rtf(file_content)

            # Ensure every channel has the is_default field (parsers that don't set it → False)
            for section in parsed_data.values():
                for ch in section:
                    ch.setdefault('is_default', False)

            self.send_json({
                'success': True,
                'sections': parsed_data,  # Send all sections
                'counts': {
                    'inputs': len(parsed_data['inputs']),
                    'aux': len(parsed_data['aux']),
                    'groups': len(parsed_data['groups']),
                    'matrix': len(parsed_data['matrix'])
                }
            })
            
        except Exception as e:
            self.send_json({'success': False, 'error': str(e)})
    
    def handle_generate(self):
        """Generate template from selected channels"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())
            
            channels = data.get('channels', [])
            stereo_mode = data.get('stereo_mode', 'split')

            if not channels:
                self.send_json({'success': False, 'error': 'No channels selected'})
                return

            # Generate Reaper template
            template = generate_reaper_track_template(channels, stereo_mode)
            
            self.send_json({
                'success': True,
                'template': template,
                'count': len(channels)
            })
            
        except Exception as e:
            self.send_json({'success': False, 'error': str(e)})
    
    def send_json(self, data):
        """Send JSON response"""
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


class ConsoleToReaperApp(rumps.App):
    def __init__(self):
        super(ConsoleToReaperApp, self).__init__("🎛️", quit_button=None)
        self.server = None
        self.server_thread = None
        self.port = None
        
        # Menu items
        self.menu = [
            rumps.MenuItem("Open Converter", callback=self.open_browser),
            rumps.separator,
            rumps.MenuItem("Restart Server", callback=self.restart_server),
            rumps.separator,
            rumps.MenuItem("Quit", callback=self.quit_app)
        ]
        
        # Start server
        self.start_server()
    
    def start_server(self):
        """Start the HTTP server in a background thread"""
        self.port = find_available_port(8081)
        
        if self.port is None:
            rumps.alert(
                title="Console to Reaper",
                message="Could not find an available port (8081-8090 all in use).\n\nPlease close other applications and try again.",
                ok="Quit"
            )
            rumps.quit_application()
            return
        
        self.server = HTTPServer(('localhost', self.port), DiGiCoToReaperHandler)
        
        # Run server in background thread
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        
        # Update menu title with port
        self.title = f"🎛️ :{self.port}"
        
        # Update menu item
        self.menu["Open Converter"].title = f"Open Converter (:{self.port})"
        
        print(f"✅ Server running on http://localhost:{self.port}")
        
        # Auto-open browser on first launch
        self.open_browser(None)
    
    def open_browser(self, _):
        """Open the converter in default browser"""
        if self.port:
            subprocess.Popen(['open', f'http://localhost:{self.port}'])
    
    def restart_server(self, _):
        """Restart the server"""
        if self.server:
            self.server.shutdown()
            self.server_thread.join(timeout=2)
        
        rumps.notification(
            title="DiGiCo to Reaper",
            subtitle="Restarting server...",
            message=""
        )
        
        self.start_server()
        
        rumps.notification(
            title="DiGiCo to Reaper",
            subtitle="Server restarted",
            message=f"Running on port {self.port}"
        )
    
    def quit_app(self, _):
        """Quit the application"""
        if self.server:
            self.server.shutdown()
        rumps.quit_application()


if __name__ == "__main__":
    ConsoleToReaperApp().run()
