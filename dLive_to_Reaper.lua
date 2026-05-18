-- Allen & Heath dLive to Reaper — Input Channel Importer
-- Parses an A&H dLive .tar.gz show file and creates tracks for all Input Channels.
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign shortcut: Actions > Action List → find script → Add shortcut → Cmd+Shift+D

-- ============================================================
-- PYTHON PARSER (embedded — requires Python 3)
-- ============================================================

local PYTHON_SCRIPT = [[
import sys, io, tarfile

with open(sys.argv[1], 'rb') as fh:
    content = fh.read()

try:
    outer = tarfile.open(fileobj=io.BytesIO(content), mode='r:gz')
except Exception:
    sys.exit(1)

scene_gz = None
for m in outer.getmembers():
    if 'StageBoxScene65535' in m.name and m.name.endswith('.tar.gz'):
        scene_gz = outer.extractfile(m).read()
        break
outer.close()

if scene_gz is None:
    sys.exit(1)

try:
    inner = tarfile.open(fileobj=io.BytesIO(scene_gz), mode='r:gz')
except Exception:
    sys.exit(1)

dat = None
for m in inner.getmembers():
    if m.name.endswith('.dat'):
        dat = inner.extractfile(m).read()
        break
inner.close()

if dat is None:
    sys.exit(1)

SECTION = b'#Input Channel Name Colour Manager'
ALL_NEXT = [
    b'Mono Group Channel Name Colour Manager',
    b'Stereo Group Channel Name Colour Manager',
    b'Mono Aux Channel Name Colour Manager',
    b'Stereo Aux Channel Name Colour Manager',
    b'Mono FX Send Channel Name Colour Manager',
    b'Stereo FX Send Channel Name Colour Manager',
    b'Main Channel Name Colour Manager',
    b'Mono Matrix Channel Name Colour Manager',
    b'Stereo Matrix Channel Name Colour Manager',
    b'FX Return Channel Name Colour Manager',
    b'DCA Channel Name Colour Manager',
    b'Monitor Channel Name Colour Manager',
]

pos = dat.find(SECTION + b'\x00')
if pos < 0:
    sys.exit(1)

data_start = pos + len(SECTION) + 1
next_pos = len(dat)
for ns in ALL_NEXT:
    p = dat.find(ns, data_start)
    if p > 0:
        next_pos = min(next_pos, p)

count = min((next_pos - data_start) // 9, 256)

for i in range(count):
    rec = dat[data_start + i*9: data_start + i*9 + 9]
    if len(rec) < 9:
        break
    name_bytes = rec[1:9]
    null = name_bytes.find(0)
    name = name_bytes[:null if null >= 0 else 8].decode('ascii', errors='replace').strip()
    if not name or not name.isprintable():
        break
    if name.isdigit():
        name = 'Input ' + str(i + 1)
    print(name)
]]

-- ============================================================
-- PARSING — shells to Python
-- ============================================================

local function parse_input_channels(filepath)
    local tmp = reaper.GetResourcePath() .. "/reaper_dlive_parse.py"
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

local INPUT_COLOR = { r=76, g=175, b=80 }  -- A&H green

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
    reaper.Undo_EndBlock("dLive: Import input channels", -1)
end

-- ============================================================
-- MAIN
-- ============================================================

local ok, filepath = reaper.GetUserFileNameForRead("", "Select A&H dLive Show File (.tar.gz)", "")
if not ok then return end

local channels, err = parse_input_channels(filepath)

if not channels then
    reaper.ShowMessageBox("Error: " .. tostring(err), "dLive to Reaper", 0)
    return
end

if #channels == 0 then
    reaper.ShowMessageBox(
        "No input channels found.\n\nMake sure this is an A&H dLive show file (.tar.gz).",
        "dLive to Reaper", 0)
    return
end

create_tracks(channels)
reaper.ShowMessageBox(string.format("Imported %d input channels.", #channels), "dLive to Reaper", 0)
