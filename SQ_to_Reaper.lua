-- Allen & Heath SQ to Reaper — Input Channel Importer
-- Parses an A&H SQ scene .DAT file and creates tracks for all Input Channels.
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign shortcut: Actions > Action List → find script → Add shortcut

-- ============================================================
-- PYTHON PARSER (embedded — requires Python 3)
-- ============================================================

local PYTHON_SCRIPT = [[
import sys

with open(sys.argv[1], 'rb') as fh:
    content = fh.read()

if len(content) != 131072 or content[:2] != b'\xa1\x00':
    sys.exit(1)

RECORD_OFFSET = 880
RECORD_STRIDE = 336

def read_name(rec_idx):
    off = RECORD_OFFSET + rec_idx * RECORD_STRIDE + 4
    raw = content[off:off + 8]
    null = raw.find(0)
    try:
        return raw[:null if null >= 0 else 8].decode('ascii').strip()
    except UnicodeDecodeError:
        return ''

input_n = 0
for i in range(48):
    in_stereo_zone = (i >= 40)
    if in_stereo_zone and i % 2 == 1:
        continue  # R side of stereo pair — skip
    input_n += 1
    name = read_name(i) or f'Input {input_n}'
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
    local tmp = reaper.GetResourcePath() .. "/reaper_sq_parse.py"
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

local INPUT_COLOR = { r=0, g=150, b=136 }  -- A&H teal

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
    reaper.Undo_EndBlock("SQ: Import input channels", -1)
end

-- ============================================================
-- MAIN
-- ============================================================

local ok, filepath = reaper.GetUserFileNameForRead("", "Select A&H SQ Scene File (.dat)", "dat")
if not ok then return end

local channels, err = parse_input_channels(filepath)

if not channels then
    reaper.ShowMessageBox("Error: " .. tostring(err), "SQ to Reaper", 0)
    return
end

if #channels == 0 then
    reaper.ShowMessageBox(
        "No input channels found.\n\nMake sure this is an A&H SQ scene file (.dat).",
        "SQ to Reaper", 0)
    return
end

create_tracks(channels)
reaper.ShowMessageBox(string.format("Imported %d input channels.", #channels), "SQ to Reaper", 0)
