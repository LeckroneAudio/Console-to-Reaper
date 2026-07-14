-- DiGiCo to Reaper — Input Channel Importer
-- Parses a DiGiCo show file (.ses binary) or session report (.rtf)
-- and creates tracks for all Input Channels.
-- Stereo channels (suffix 's') are split into two mono tracks (Name L / Name R).
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign shortcut: Actions > Action List → find script → Add shortcut → Cmd+Shift+I

-- ============================================================
-- UTILITIES
-- ============================================================

local function split_str(str, sep)
    local result, i, sep_len = {}, 1, #sep
    while true do
        local j = str:find(sep, i, true)
        if j then
            result[#result + 1] = str:sub(i, j - 1)
            i = j + sep_len
        else
            result[#result + 1] = str:sub(i)
            break
        end
    end
    return result
end

local function trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

local function clean_rtf_field(s)
    s = s:gsub("\\'[%x][%x]", "")
    s = s:gsub("\\[%a]+%-?[%d]*%s?", "")
    s = s:gsub("\\[^%a%d%s]", "")
    s = s:gsub("[{}]", "")
    s = s:gsub("%s+", " ")
    return trim(s)
end

-- ============================================================
-- PARSER — RTF SESSION REPORT
-- ============================================================

local function parse_rtf_inputs(content)
    local channels = {}
    local lines = split_str(content, "\\par")
    local in_section, found_header = false, false

    for _, line in ipairs(lines) do
        if line:find("Input Channels", 1, true) and line:find("\\b", 1, true) then
            in_section, found_header = true, false
        elseif in_section and (
            line:find("Aux Outputs",    1, true) or
            line:find("Group Outputs",  1, true) or
            line:find("Matrix Outputs", 1, true) or
            line:find("Matrix Inputs",  1, true) or
            line:find("Control Groups", 1, true) or
            line:find("VCA Groups",     1, true)
        ) then
            break
        end

        if in_section and not found_header then
            if line:lower():find("name", 1, true) then found_header = true end
            goto continue
        end

        if in_section and found_header then
            local parts = split_str(line, "\\tab")
            if #parts >= 2 then
                local num  = clean_rtf_field(parts[1])
                local name = clean_rtf_field(parts[2])
                local ch   = num:match("^(%d+s?)$")

                if ch and name ~= "" and name:lower() ~= "name" then
                    local dup = false
                    for _, existing in ipairs(channels) do
                        if existing.number == ch then dup = true; break end
                    end
                    if not dup then
                        channels[#channels + 1] = { number=ch, name=name }
                    end
                end
            end
        end

        ::continue::
    end

    return channels
end

-- ============================================================
-- PARSER — SES BINARY FORMAT
-- ============================================================

local function parse_ses_inputs(content)
    if content:sub(1, 7) ~= "DiGiCo " then return nil end

    -- off is 0-indexed; Lua strings are 1-indexed
    local function get_name(off)
        local field = content:sub(off + 1, off + 32)
        local null_pos = field:find("\0", 1, true)
        if null_pos then field = field:sub(1, null_pos - 1) end
        return field
    end

    -- Returns 0-indexed data offset and channel count for a labeled section.
    -- Count is LE16 at label+106,+107; data starts 119 bytes after the label.
    local function find_section(label)
        local pos = content:find(label, 1, true)  -- 1-indexed
        if not pos then return nil, nil, nil end
        local b1 = content:byte(pos + 106)
        local b2 = content:byte(pos + 107)
        return pos, pos + 118, b1 + b2 * 256  -- label pos, 0-indexed data offset, count
    end

    -- Labeled sections hold counts/stereo flags, but their name fields are
    -- display buffers overwritten in place without clearing — short names
    -- keep the tail of the old name ('RC' over 'Ch 28' reads 'RC 28').
    -- Clean copies of current names exist elsewhere in the file.
    local excl_lo, excl_hi = math.huge, 0
    for _, label in ipairs({"Aux Outputs", "Matrix Outputs", "Group Outputs", "Input Channels"}) do
        local p = content:find(label, 1, true)
        if p then
            if p < excl_lo then excl_lo = p end
            if p > excl_hi then excl_hi = p end
        end
    end
    if excl_hi == 0 then return nil end
    excl_lo = excl_lo - 1000
    excl_hi = excl_hi + 100 * 92 + 2000

    -- True if name appears null-terminated outside the labeled sections
    local function in_dict(name)
        local pat = name .. "\0"
        local s = 1
        while true do
            local p = content:find(pat, s, true)
            if not p then return false end
            if p < excl_lo or p > excl_hi then return true end
            s = p + 1
        end
    end

    -- Recover the true name from a dirty display buffer
    local function clean_name(stored, n)
        stored = stored:gsub("%s+$", "")
        if stored == "" or stored:match("^Ch %d+$") then
            return stored, true
        end
        -- 1) name written over this channel's default ('Ch 27' -> 'Evan7')
        local D = "Ch " .. n
        if #D == #stored then
            for k = 2, #stored - 1 do
                if stored:sub(k + 1) == D:sub(k + 1) and stored:sub(1, k) ~= D:sub(1, k) then
                    return (stored:sub(1, k):gsub("%s+$", "")), false
                end
            end
        end
        -- 2) longest prefix that exists null-terminated elsewhere
        for k = #stored, 2, -1 do
            if in_dict(stored:sub(1, k)) then
                local got = stored:sub(1, k):gsub("%s+$", "")
                -- 3) letter-digit junction with digitless form on file
                if got == stored then
                    local base, digits = stored:match("^(.-%a)(%d%d?)$")
                    if base and in_dict(base) then
                        return base, false
                    end
                end
                return got, false
            end
        end
        return stored, false
    end

    -- Find the 212-stride state block matching the labeled input names.
    -- True names are always prefixes of the dirty labeled names, so the
    -- block scoring counts prefix-compatible records.
    local STRIDE = 212
    local function find_best_block(labeled)
        local first = labeled[1]
        local cands, n_cands = {}, 0
        for k = #first, 2, -1 do
            local pat = first:sub(1, k) .. "\0"
            local s = 1
            while n_cands < 400 do
                local p = content:find(pat, s, true)
                if not p then break end
                if (p < excl_lo or p > excl_hi) and not cands[p] then
                    cands[p] = true
                    n_cands = n_cands + 1
                end
                s = p + 1
            end
        end
        local best_score, best_off = 0, nil
        for p in pairs(cands) do
            local score = 0
            for i, L in ipairs(labeled) do
                local b = get_name((p - 1) + (i - 1) * STRIDE)
                if b ~= "" and (L == b or L:sub(1, #b) == b) then
                    score = score + 1
                end
            end
            if score > best_score or (score == best_score and (not best_off or p > best_off)) then
                best_score, best_off = score, p
            end
        end
        return best_score, best_off  -- best_off is 1-indexed
    end

    local _, ic_off, inp_count = find_section("Input Channels")
    if not ic_off or not inp_count or inp_count == 0 then return nil end

    local labeled, i_stereo = {}, {}
    for i = 0, inp_count - 1 do
        labeled[i + 1] = get_name(ic_off + i * 92)
        i_stereo[i + 1] = (content:byte(ic_off + i * 92 + 33) == 0x02)
    end

    local score, blk = find_best_block(labeled)
    local use_block = blk and score >= inp_count * 0.6

    local channels = {}
    for i = 1, inp_count do
        local name
        if use_block then
            name = get_name((blk - 1) + (i - 1) * STRIDE):gsub("%s+$", "")
        else
            name = clean_name(labeled[i], i)
        end
        if name ~= "" then
            channels[#channels + 1] = { number = i .. (i_stereo[i] and "s" or ""), name = name }
        end
    end

    return channels
end

-- ============================================================
-- TRACK CREATION
-- ============================================================

local INPUT_COLOR = { r=142, g=142, b=147 }  -- #8E8E93 gray

-- stereo_mode: "split" = stereo channels become two mono tracks (Name L / Name R);
--              "stereo" = one track with a stereo hardware input pair.
local function create_tracks(channels, stereo_mode)
    local proj = 0
    local n    = 0
    local hw   = 0  -- 0-based hardware input counter

    reaper.Undo_BeginBlock()
    reaper.PreventUIRefresh(1)

    local color = reaper.ColorToNative(INPUT_COLOR.r, INPUT_COLOR.g, INPUT_COLOR.b)

    local function add_track(nm, recinput, chans)
        local idx = reaper.CountTracks(proj)
        reaper.InsertTrackAtIndex(idx, true)
        local tr = reaper.GetTrack(proj, idx)
        reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", nm, true)
        reaper.SetTrackColor(tr, color)
        reaper.SetMediaTrackInfo_Value(tr, "I_RECINPUT", recinput)
        if chans then reaper.SetMediaTrackInfo_Value(tr, "I_NCHAN", chans) end
        n = n + 1
    end

    for _, ch in ipairs(channels) do
        local is_stereo = ch.number:sub(-1) == "s"
        if is_stereo and stereo_mode == "stereo" then
            -- stereo input pair encoded as 1024 + left input index
            add_track(ch.name, 1024 + hw, 2)
            hw = hw + 2
        elseif is_stereo then
            add_track(ch.name .. " L", hw); hw = hw + 1
            add_track(ch.name .. " R", hw); hw = hw + 1
        else
            add_track(ch.name, hw); hw = hw + 1
        end
    end

    reaper.TrackList_AdjustWindows(false)
    reaper.UpdateArrange()
    reaper.PreventUIRefresh(-1)
    reaper.Undo_EndBlock("DiGiCo: Import input channels", -1)
    return n
end

-- ============================================================
-- MAIN
-- ============================================================

local ok, filepath = reaper.GetUserFileNameForRead("", "Select DiGiCo Show File (.ses or .rtf)", "ses")
if not ok then return end

local is_ses = filepath:lower():match("%.ses$") ~= nil
local mode   = is_ses and "rb" or "r"

local f, err = io.open(filepath, mode)
if not f then
    reaper.ShowMessageBox("Could not open file:\n" .. tostring(err), "DiGiCo to Reaper", 0)
    return
end
local content = f:read("*all"); f:close()

if not content or content == "" then
    reaper.ShowMessageBox("File is empty or could not be read.", "DiGiCo to Reaper", 0)
    return
end

local channels
if is_ses then
    channels = parse_ses_inputs(content)
    if not channels then
        reaper.ShowMessageBox(
            "Not a valid DiGiCo SD show file.\n\nMake sure this is a .ses file saved from\na DiGiCo SD-series console.",
            "DiGiCo to Reaper", 0)
        return
    end
else
    channels = parse_rtf_inputs(content)
end

if #channels == 0 then
    local hint = is_ses
        and "No named input channels found in this show file."
        or  "No input channels found.\n\nMake sure this is a DiGiCo RTF session report\n(File > Print Session Report on the console)."
    reaper.ShowMessageBox(hint, "DiGiCo to Reaper", 0)
    return
end

-- Ask how to handle stereo channels (only if any exist)
local stereo_mode = "split"
local has_stereo = false
for _, ch in ipairs(channels) do
    if ch.number:sub(-1) == "s" then has_stereo = true; break end
end
if has_stereo then
    -- 3 = Yes / No / Cancel;  6 = Yes, 7 = No, 2 = Cancel
    local ret = reaper.ShowMessageBox(
        "This file contains stereo channels.\n\nSplit them into L / R mono tracks?\n\nYes = split to L/R mono\nNo = keep as stereo tracks",
        "DiGiCo to Reaper", 3)
    if ret == 2 then return end
    stereo_mode = (ret == 6) and "split" or "stereo"
end

create_tracks(channels, stereo_mode)
