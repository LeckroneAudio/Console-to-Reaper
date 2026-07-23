-- Yamaha DM7 to Reaper — Input Channel Importer
-- Parses a Yamaha DM7 .dm7f project file and creates tracks for all Input Channels.
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign shortcut: Actions > Action List → find script → Add shortcut

-- ============================================================
-- PYTHON PARSER (embedded — requires Python 3)
-- ============================================================

local PYTHON_SCRIPT = [[
import sys, zlib, struct

with open(sys.argv[1], 'rb') as fh:
    content = fh.read()

mixing_data = None
for i in range(len(content) - 1):
    b0, b1 = content[i], content[i + 1]
    if b0 == 0x78 and b1 in (0x01, 0x9C, 0xDA):
        try:
            dec = zlib.decompress(content[i:])
            if dec[72:90] == b'#MMS FIELD\x00\x00Mixing' and b'COL0InputChannel' in dec[:600]:
                mixing_data = dec
                break
        except Exception:
            pass

if mixing_data is None:
    sys.exit(1)

col0_pos = mixing_data.find(b'COL0InputChannel')
if col0_pos < 0:
    sys.exit(1)

inp_record_size = struct.unpack_from('<I', mixing_data, col0_pos + 40)[0]
inp_count       = struct.unpack_from('<I', mixing_data, col0_pos + 44)[0]

stereo_off = mixing_data.find(b'STEREO\x00\x00')
if stereo_off < 0 or inp_record_size == 0:
    sys.exit(1)

inp_num = 0
for i in range(inp_count):
    name_off = stereo_off + 8 + i * inp_record_size
    if name_off + 64 > len(mixing_data):
        break
    if mixing_data[name_off - 8:name_off] != b'STEREO\x00\x00':
        break
    name = mixing_data[name_off:name_off + 64].rstrip(b'\x00').decode('ascii', errors='replace').strip()
    inp_num += 1
    if not name:
        name = f'Ch {inp_num:02d}'
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
    local tmp = reaper.GetResourcePath() .. "/reaper_dm7_parse.py"
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

local INPUT_COLOR = { r=63, g=81, b=181 }  -- DM7 indigo (distinct from Rivage blue)

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
    reaper.Undo_EndBlock("DM7: Import input channels", -1)
end

-- ============================================================
-- MAIN
-- ============================================================

local ok, filepath = reaper.GetUserFileNameForRead("", "Select Yamaha DM7 Project File (.dm7f)", "dm7f")
if not ok then return end

local channels, err = parse_input_channels(filepath)

if not channels then
    reaper.ShowMessageBox("Error: " .. tostring(err), "DM7 to Reaper", 0)
    return
end

if #channels == 0 then
    reaper.ShowMessageBox(
        "No input channels found.\n\nMake sure this is a Yamaha DM7 project file (.dm7f).",
        "DM7 to Reaper", 0)
    return
end

create_tracks(channels)
reaper.ShowMessageBox(string.format("Imported %d input channels.", #channels), "DM7 to Reaper", 0)
