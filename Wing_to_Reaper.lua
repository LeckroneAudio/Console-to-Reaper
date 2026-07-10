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

local function parse_input_channels(filepath)
    local tmp = reaper.GetResourcePath() .. "/reaper_wing_parse.py"
    local f = io.open(tmp, "w")
    if not f then return nil, "Could not write temp file" end
    f:write(PYTHON_SCRIPT)
    f:close()

    local cmd = string.format('python3 "%s" "%s" 2>/dev/null', tmp, filepath)
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
