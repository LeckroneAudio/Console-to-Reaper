-- Avid S6L / VENUE to Reaper — Input Channel Importer
-- Parses an S6L .dsh show file and creates tracks for all Input Channels.
-- Requires Python 3 (comes with the Console to Reaper app).
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign shortcut: Actions > Action List → find script → Add shortcut → Cmd+Shift+6

-- ============================================================
-- PYTHON PARSER (embedded — requires Python 3)
-- ============================================================

local PYTHON_SCRIPT = [[
import sys, re

with open(sys.argv[1], 'rb') as fh:
    data = fh.read()

strip_name_pat = re.compile(rb'\nStrip\x00\x0d.{5}([\x20-\x7e]+)\x00')
seen = set()

for m in strip_name_pat.finditer(data):
    name = m.group(1).decode('ascii', errors='replace').strip()
    if not name:
        continue
    if name in seen:
        break  # duplicate = new snapshot, stop
    seen.add(name)

    pos = m.start()
    chunk = data[max(0, pos - 2000):pos]
    inp_dist = len(chunk) - chunk.rfind(b'\nInputStrip\x00')
    bus_dist  = len(chunk) - max(
        chunk.rfind(b'\nAudioMasterStrip\x00'),
        chunk.rfind(b'\nBusMasterStrip\x00')
    )

    if inp_dist < bus_dist:
        print(name)
]]

-- ============================================================
-- PARSING — shells to Python
-- ============================================================

local function parse_input_channels(filepath)
    local tmp = reaper.GetResourcePath() .. "/reaper_s6l_parse.py"
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

local INPUT_COLOR = { r=0, g=130, b=200 }  -- S6L blue

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
    reaper.Undo_EndBlock("S6L: Import input channels", -1)
end

-- ============================================================
-- MAIN
-- ============================================================

local ok, filepath = reaper.GetUserFileNameForRead("", "Select S6L Show File (.dsh)", "dsh")
if not ok then return end

local channels, err = parse_input_channels(filepath)

if not channels then
    reaper.ShowMessageBox("Error: " .. tostring(err), "S6L to Reaper", 0)
    return
end

if #channels == 0 then
    reaper.ShowMessageBox(
        "No input channels found.\n\nMake sure this is an Avid S6L / VENUE .dsh show file.",
        "S6L to Reaper", 0)
    return
end

create_tracks(channels)
reaper.ShowMessageBox(string.format("Imported %d input channels.", #channels), "S6L to Reaper", 0)
