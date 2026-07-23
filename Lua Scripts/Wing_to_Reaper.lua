-- Behringer Wing to Reaper — Input Channel Importer
-- Parses a Wing .snap snapshot file and creates tracks for all Input Channels.
-- Requires Python 3 (comes with the Console to Reaper app).
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign shortcut: Actions > Action List → find script → Add shortcut → Cmd+Shift+W

-- ============================================================
-- PYTHON PARSER (embedded — requires Python 3)
-- ============================================================

local PYTHON_SCRIPT = [[
import sys, json

with open(sys.argv[1], 'r', encoding='utf-8', errors='ignore') as fh:
    data = json.load(fh)

ae = data.get('ae_data') or data
lcl = ae.get('io', {}).get('in', {}).get('LCL', {})

for k, v in sorted(lcl.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
    if not isinstance(v, dict):
        continue
    num = int(k) if k.isdigit() else 0
    name = v.get('name', '').strip()
    if not name:
        name = 'Ch {:02d}'.format(num)
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
    local tmp = reaper.GetResourcePath() .. "/reaper_wing_parse.py"
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

local INPUT_COLOR = { r=156, g=39, b=176 }  -- Wing purple

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
    reaper.Undo_EndBlock("Wing: Import input channels", -1)
end

-- ============================================================
-- MAIN
-- ============================================================

local ok, filepath = reaper.GetUserFileNameForRead("", "Select Wing Snapshot File (.snap)", "snap")
if not ok then return end

local channels, err = parse_input_channels(filepath)

if not channels then
    reaper.ShowMessageBox("Error: " .. tostring(err), "Wing to Reaper", 0)
    return
end

if #channels == 0 then
    reaper.ShowMessageBox(
        "No input channels found.\n\nMake sure this is a Behringer Wing .snap snapshot file.",
        "Wing to Reaper", 0)
    return
end

create_tracks(channels)
reaper.ShowMessageBox(string.format("Imported %d input channels.", #channels), "Wing to Reaper", 0)
