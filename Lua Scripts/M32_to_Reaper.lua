-- Behringer X32 / Midas M32 to Reaper — Input Channel Importer
-- Parses an X32/M32 .scn scene file and creates tracks for all Input Channels.
-- Stereo channels (suffix 's') are split into two mono tracks (Name L / Name R).
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign shortcut: Actions > Action List → find script → Add shortcut → Cmd+Shift+M

-- ============================================================
-- PARSER — /ch/ input channels only
-- ============================================================

local function parse_input_channels(content)
    local channels = {}
    local seen = {}

    for line in content:gmatch("[^\r\n]+") do
        -- Match: /ch/01/config "Name" icon COLOR channel_num
        local num_str, name = line:match("^/ch/(%d+)/config%s+\"([^\"]*)\"")
        if num_str then
            local num = tonumber(num_str)
            if name == "" then name = string.format("Ch %02d", num) end
            if not seen[num] then
                seen[num] = true
                channels[#channels + 1] = { number = num, name = name }
            end
        end
    end

    -- Sort by channel number
    table.sort(channels, function(a, b) return a.number < b.number end)
    return channels
end

-- ============================================================
-- TRACK CREATION
-- ============================================================

local INPUT_COLOR = { r=230, g=126, b=34 }  -- X32/M32 orange

local function create_tracks(channels)
    local proj      = 0
    local first_idx = reaper.CountTracks(proj)
    local color     = reaper.ColorToNative(INPUT_COLOR.r, INPUT_COLOR.g, INPUT_COLOR.b)

    reaper.Undo_BeginBlock()
    reaper.PreventUIRefresh(1)

    for _, ch in ipairs(channels) do
        local is_stereo = tostring(ch.number):sub(-1) == "s"
        local names     = is_stereo and { ch.name .. " L", ch.name .. " R" } or { ch.name }

        for _, nm in ipairs(names) do
            local idx = reaper.CountTracks(proj)
            reaper.InsertTrackAtIndex(idx, true)
            local tr = reaper.GetTrack(proj, idx)
            reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", nm, true)
            reaper.SetTrackColor(tr, color)
        end
    end

    for i = first_idx, reaper.CountTracks(proj) - 1 do
        reaper.SetMediaTrackInfo_Value(reaper.GetTrack(proj, i), "I_RECINPUT", i)
    end

    reaper.TrackList_AdjustWindows(false)
    reaper.UpdateArrange()
    reaper.PreventUIRefresh(-1)
    reaper.Undo_EndBlock("X32/M32: Import input channels", -1)
end

-- ============================================================
-- MAIN
-- ============================================================

local ok, filepath = reaper.GetUserFileNameForRead("", "Select X32/M32 Scene File (.scn)", "scn")
if not ok then return end

local f, err = io.open(filepath, "r")
if not f then
    reaper.ShowMessageBox("Could not open file:\n" .. tostring(err), "X32/M32 to Reaper", 0)
    return
end
local content = f:read("*all"); f:close()

if not content or content == "" then
    reaper.ShowMessageBox("File is empty or could not be read.", "X32/M32 to Reaper", 0)
    return
end

local channels = parse_input_channels(content)

if #channels == 0 then
    reaper.ShowMessageBox(
        "No input channels found.\n\nMake sure this is an X32/M32 .scn scene file.",
        "X32/M32 to Reaper", 0)
    return
end

create_tracks(channels)
reaper.ShowMessageBox(string.format("Imported %d input channels.", #channels), "X32/M32 to Reaper", 0)
