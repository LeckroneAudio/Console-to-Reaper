-- Yamaha Rivage PM to Reaper — Input Channel Importer
-- Parses a Yamaha Rivage PM .RIVAGEPM show file and creates tracks for all Input Channels.
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign shortcut: Actions > Action List → find script → Add shortcut → Cmd+Shift+R

-- ============================================================
-- PYTHON PARSER (embedded — requires Python 3)
-- ============================================================

local PYTHON_SCRIPT = [[
import sys, zlib, struct

with open(sys.argv[1], 'rb') as fh:
    content = fh.read()

raw = None
for pos in range(0, len(content) - 2):
    if content[pos:pos+2] in (b'\x78\x01', b'\x78\x9c', b'\x78\xda'):
        try:
            candidate = zlib.decompress(content[pos:pos+200000])
            if b'EN00/mix' in candidate[:256]:
                raw = candidate
                break
        except Exception:
            continue

if raw is None:
    sys.exit(1)

schema = []
pos = 196
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
info = None
for i, entry in enumerate(schema):
    if entry['kind'] == 'COL0' and entry['name'] == 'InputChannel' and entry['v'][4] >= 1:
        for j in range(i + 1, min(i + 60, len(schema))):
            if schema[j]['kind'] == 'COL0' and schema[j]['name'] == 'Label':
                info = {'data_offset': entry['v'][2], 'rec_size': entry['v'][3],
                        'count': entry['v'][4], 'name_offset': schema[j]['v'][2]}
                break
        if info:
            break

if not info:
    sys.exit(1)

sect_start = data_start + info['data_offset']
seen = set()
for i in range(info['count']):
    p = sect_start + i * info['rec_size'] + info['name_offset']
    nb = raw[p:p+64]
    null = nb.find(0)
    name = nb[:null if null >= 0 else 64].decode('ascii', errors='replace').strip()
    if name and name not in seen:
        seen.add(name)
        print(name)
]]

-- ============================================================
-- PARSING — shells to Python
-- ============================================================

local function is_windows()
    local os_str = reaper.GetOS and reaper.GetOS() or ""
    return os_str:find("Win") ~= nil
end

-- Find a Python 3 interpreter: `py -3`/`python` on Windows, `python3` on
-- macOS/Linux. Returns nil if none found.
local function find_python()
    if is_windows() then
        local chk = io.popen('where py 2>NUL', "r")
        local py = chk and chk:read("*l") or nil
        if chk then chk:close() end
        if py and py ~= "" then return "py -3" end

        chk = io.popen('where python 2>NUL', "r")
        py = chk and chk:read("*l") or nil
        if chk then chk:close() end
        if py and py ~= "" then return "python" end
        return nil
    else
        local chk = io.popen('command -v python3 2>/dev/null', "r")
        local py = chk and chk:read("*l") or nil
        if chk then chk:close() end
        if py and py ~= "" then return "python3" end
        return nil
    end
end

local function parse_input_channels(filepath)
    local tmp = reaper.GetResourcePath() .. "/reaper_rivage_parse.py"
    local f = io.open(tmp, "w")
    if not f then return nil, "Could not write temp file" end
    f:write(PYTHON_SCRIPT)
    f:close()

    local py = find_python()
    if not py then
        os.remove(tmp)
        return nil, "No Python 3 interpreter found. Install Python 3 " ..
            "(python.org on Windows, or run 'xcode-select --install' on macOS)."
    end

    local null_redirect = is_windows() and "2>NUL" or "2>/dev/null"
    local cmd = string.format('%s "%s" "%s" %s', py, tmp, filepath, null_redirect)
    local pipe = io.popen(cmd)
    if not pipe then
        os.remove(tmp)
        return nil, "Could not run Python 3"
    end

    local channels = {}
    for line in pipe:lines() do
        local name = line:match("^(.-)%s*$")
        if name ~= "" then channels[#channels + 1] = name end
    end
    pipe:close()
    os.remove(tmp)
    return channels
end

-- ============================================================
-- TRACK CREATION
-- ============================================================

local INPUT_COLOR = { r=91, g=192, b=235 }  -- Yamaha blue

local function create_tracks(channels)
    local proj      = 0
    local first_idx = reaper.CountTracks(proj)
    local color     = reaper.ColorToNative(INPUT_COLOR.r, INPUT_COLOR.g, INPUT_COLOR.b)

    reaper.Undo_BeginBlock()
    reaper.PreventUIRefresh(1)

    for _, name in ipairs(channels) do
        local idx = reaper.CountTracks(proj)
        reaper.InsertTrackAtIndex(idx, true)
        local tr = reaper.GetTrack(proj, idx)
        reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", name, true)
        reaper.SetTrackColor(tr, color)
    end

    for i = first_idx, reaper.CountTracks(proj) - 1 do
        reaper.SetMediaTrackInfo_Value(reaper.GetTrack(proj, i), "I_RECINPUT", i)
    end

    reaper.TrackList_AdjustWindows(false)
    reaper.UpdateArrange()
    reaper.PreventUIRefresh(-1)
    reaper.Undo_EndBlock("Rivage PM: Import input channels", -1)
end

-- ============================================================
-- MAIN
-- ============================================================

local ok, filepath = reaper.GetUserFileNameForRead("", "Select Yamaha Rivage PM Show File (.RIVAGEPM)", "RIVAGEPM")
if not ok then return end

local channels, err = parse_input_channels(filepath)

if not channels then
    reaper.ShowMessageBox("Error: " .. tostring(err), "Rivage PM to Reaper", 0)
    return
end

if #channels == 0 then
    reaper.ShowMessageBox(
        "No input channels found.\n\nMake sure this is a Yamaha Rivage PM show file (.RIVAGEPM).",
        "Rivage PM to Reaper", 0)
    return
end

create_tracks(channels)
reaper.ShowMessageBox(string.format("Imported %d input channels.", #channels), "Rivage PM to Reaper", 0)
