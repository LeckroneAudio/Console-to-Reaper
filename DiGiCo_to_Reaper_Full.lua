-- DiGiCo to Reaper — Full Interactive Importer
-- Parses a DiGiCo show file (.ses binary or .rtf session report) and shows
-- an interactive channel selector for all sections before creating tracks.
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign a shortcut in the Action List if desired

-- ============================================================
-- UTILITIES
-- ============================================================

local function split_str(str, sep)
    local result, i, sep_len = {}, 1, #sep
    while true do
        local j = str:find(sep, i, true)
        if j then result[#result+1] = str:sub(i,j-1); i = j+sep_len
        else   result[#result+1] = str:sub(i); break end
    end
    return result
end

local function trim(s) return (s:gsub("^%s+",""):gsub("%s+$","")) end

local function clean_rtf(s)
    s = s:gsub("\\'[%x][%x]","")
    s = s:gsub("\\[%a]+%-?[%d]*%s?","")
    s = s:gsub("\\[^%a%d%s]","")
    s = s:gsub("[{}]","")
    return trim(s:gsub("%s+"," "))
end

-- ============================================================
-- SECTION COLORS  (r, g, b  in 0-255 range)
-- ============================================================

local COL = {
    inputs = {r=142, g=142, b=147},
    aux    = {r=255, g=149, b=0  },
    groups = {r=52,  g=199, b=89 },
    matrix = {r=175, g=82,  b=222},
}
local SEC_ORDER  = {"inputs","aux","groups","matrix"}
local SEC_LABELS = {
    inputs="INPUT CHANNELS", aux="AUX OUTPUTS",
    groups="GROUPS", matrix="MATRIX OUTPUTS",
}

-- ============================================================
-- RTF PARSER — ALL SECTIONS
-- ============================================================

local function parse_rtf(content)
    local out = {inputs={}, aux={}, groups={}, matrix={}}
    local lines = split_str(content, "\\par")
    local sec, hdr = nil, false
    local def_pat = {
        inputs="^Ch %d+$", aux="^Aux %d+$",
        groups="^Gr[a-z]+ %d+$", matrix="^Matrix %d+$",
    }
    local pfix = {inputs="CH", aux="AUX", groups="GRP", matrix="MTX"}

    for _, ln in ipairs(lines) do
        if     ln:find("Input Channels", 1,true) and ln:find("\\b",1,true) then sec,hdr="inputs",false
        elseif ln:find("Aux Outputs",    1,true) and ln:find("\\b",1,true) then sec,hdr="aux",false
        elseif ln:find("Group Outputs",  1,true) and ln:find("\\b",1,true) then sec,hdr="groups",false
        elseif ln:find("Matrix Outputs", 1,true) and ln:find("\\b",1,true) then sec,hdr="matrix",false
        elseif ln:find("Matrix Inputs",  1,true) or ln:find("Control Groups",1,true) then sec=nil
        end

        if sec and not hdr then
            if ln:lower():find("name",1,true) then hdr=true end
            goto cont
        end

        if sec and hdr then
            local parts = split_str(ln,"\\tab")
            if #parts >= 2 then
                local num  = clean_rtf(parts[1])
                local name = clean_rtf(parts[2])
                local ch   = num:match("^(%d+s?)$")
                if ch and name~="" and name:lower()~="name" then
                    local full = pfix[sec]..ch
                    local dup  = false
                    for _, e in ipairs(out[sec]) do if e.number==full then dup=true; break end end
                    if not dup then
                        out[sec][#out[sec]+1] = {
                            number=full, name=name, type=sec,
                            is_default = name:match(def_pat[sec]) ~= nil,
                        }
                    end
                end
            end
        end
        ::cont::
    end
    return out
end

-- ============================================================
-- SES BINARY PARSER — ALL SECTIONS
-- ============================================================

local function parse_ses(content)
    if content:sub(1,7) ~= "DiGiCo " then return nil end
    local out = {inputs={}, aux={}, groups={}, matrix={}}

    -- off is 0-indexed; Lua strings are 1-indexed
    local function get_name(off)
        local f = content:sub(off+1, off+32)
        local z = f:find("\0",1,true)
        return z and f:sub(1,z-1) or f
    end

    -- Returns 0-indexed data start and LE16 count for a labeled section.
    local function find_sec(label)
        local pos = content:find(label,1,true)
        if not pos then return nil,nil end
        local b1,b2 = content:byte(pos+106), content:byte(pos+107)
        return pos+118, b1+b2*256
    end

    -- Labeled sections hold counts/stereo flags, but their name fields are
    -- display buffers overwritten in place without clearing — short names
    -- keep the tail of the old name ('RC' over 'Aux 6' reads 'RCx 6').
    -- Clean copies of the current names exist elsewhere in the file.
    local excl_lo, excl_hi = math.huge, 0
    for _, lbl in ipairs({"Aux Outputs","Matrix Outputs","Group Outputs","Input Channels"}) do
        local p = content:find(lbl,1,true)
        if p then
            if p < excl_lo then excl_lo = p end
            if p > excl_hi then excl_hi = p end
        end
    end
    if excl_hi == 0 then return out end
    excl_lo = excl_lo - 1000
    excl_hi = excl_hi + 100*92 + 2000

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

    local DEF_PAT = { Ch="^Ch %d+$", Aux="^Aux %d+$", Grp="^Grp %d+$", Matrix="^Matrix %d+$" }

    -- Recover the true name from a dirty display buffer.
    -- Returns name, is_default.
    local function clean_name(stored, prefix, n, stereo)
        stored = stored:gsub("%s+$","")
        if stored == "" or stored:match(DEF_PAT[prefix]) then
            return stored, true
        end
        -- 1) name written over this channel's default ('Ch 27' -> 'Evan7')
        local D = prefix .. " " .. n
        if #D == #stored then
            for k = 2, #stored - 1 do
                if stored:sub(k+1) == D:sub(k+1) and stored:sub(1,k) ~= D:sub(1,k) then
                    return (stored:sub(1,k):gsub("%s+$","")), false
                end
            end
        end
        -- 2) stereo channels have a clean '<name> R' partner leg on file
        if stereo then
            for k = #stored, 2, -1 do
                local cand = stored:sub(1,k):gsub("%s+$","")
                if in_dict(cand .. " R") then
                    return cand, false
                end
            end
        end
        -- 3) longest prefix that exists null-terminated elsewhere
        for k = #stored, 2, -1 do
            if in_dict(stored:sub(1,k)) then
                local got = stored:sub(1,k):gsub("%s+$","")
                -- 4) letter-digit junction with digitless form on file
                if got == stored then
                    local base = stored:match("^(.-%a)%d%d?$")
                    if base and in_dict(base) then
                        return base, false
                    end
                end
                return got, false
            end
        end
        return stored, false
    end

    -- Find the 212-stride state block matching a labeled section's names.
    -- True names are always prefixes of the dirty labeled names, so score
    -- candidate blocks by prefix-compatible record count.
    local STRIDE = 212
    local function find_best_block(labeled)
        local first = labeled[1]
        if not first or first == "" then return 0, nil end
        local cands, n_cands = {}, 0
        for k = #first, 2, -1 do
            local pat = first:sub(1,k) .. "\0"
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
                local b = get_name((p-1) + (i-1)*STRIDE)
                if b ~= "" and (L == b or L:sub(1,#b) == b) then
                    score = score + 1
                end
            end
            if score > best_score or (score == best_score and (not best_off or p > best_off)) then
                best_score, best_off = score, p
            end
        end
        return best_score, best_off  -- best_off is 1-indexed
    end

    -- INPUT CHANNELS
    local ic_off, inp_cnt = find_sec("Input Channels")
    if ic_off and inp_cnt and inp_cnt > 0 then
        local labeled, i_stereo = {}, {}
        for i = 0, inp_cnt-1 do
            labeled[i+1] = get_name(ic_off + i*92)
            i_stereo[i+1] = (content:byte(ic_off + i*92 + 33) == 0x02)
        end
        local score, blk = find_best_block(labeled)
        local use_block = blk and score >= inp_cnt * 0.6
        for i = 1, inp_cnt do
            local name, is_def
            if use_block then
                name = get_name((blk-1) + (i-1)*STRIDE):gsub("%s+$","")
                is_def = (name == "") or (name:match("^Ch %d+$") ~= nil)
            else
                name, is_def = clean_name(labeled[i], "Ch", i, i_stereo[i])
            end
            if name == "" then name = "Ch "..i end
            out.inputs[#out.inputs+1] = {
                number="CH"..i..(i_stereo[i] and "s" or ""),
                name=name, type="inputs", is_default=is_def,
            }
        end
    end

    -- GROUP OUTPUTS
    local g_off, g_cnt = find_sec("Group Outputs")
    if g_off and g_cnt and g_cnt > 0 then
        local labeled, g_stereo = {}, {}
        for i = 0, g_cnt-1 do
            labeled[i+1] = get_name(g_off + i*92)
            g_stereo[i+1] = (content:byte(g_off + i*92 + 33) == 0x02)
        end
        local score, blk = find_best_block(labeled)
        local use_block = blk and score >= g_cnt * 0.75
        for i = 1, g_cnt do
            local name, is_def
            if use_block then
                name = get_name((blk-1) + (i-1)*STRIDE):gsub("%s+$","")
                is_def = (name == "") or (name:match("^Grp %d+$") ~= nil)
            else
                name, is_def = clean_name(labeled[i], "Grp", i, g_stereo[i])
            end
            if name == "" then name = "Grp "..i end
            out.groups[#out.groups+1] = {
                number="GRP"..i..(g_stereo[i] and "s" or ""),
                name=name, type="groups", is_default=is_def,
            }
        end
    end

    -- AUX OUTPUTS
    local a_off, a_cnt = find_sec("Aux Outputs")
    if a_off and a_cnt then
        for i = 1, a_cnt do
            local off = a_off + (i-1)*92
            local stereo = content:byte(off+33) == 0x02
            local name, is_def = clean_name(get_name(off), "Aux", i, stereo)
            if name == "" then name = "Aux "..i end
            out.aux[#out.aux+1] = {
                number="AUX"..i..(stereo and "s" or ""),
                name=name, type="aux", is_default=is_def,
            }
        end
    end

    -- MATRIX OUTPUTS
    local m_off, m_cnt = find_sec("Matrix Outputs")
    if m_off and m_cnt then
        for i = 1, m_cnt do
            local off = m_off + (i-1)*92
            local stereo = content:byte(off+33) == 0x02
            local name, is_def = clean_name(get_name(off), "Matrix", i, stereo)
            if name == "" then name = "Matrix "..i end
            out.matrix[#out.matrix+1] = {
                number="MTX"..i..(stereo and "s" or ""),
                name=name, type="matrix", is_default=is_def,
            }
        end
    end

    return out
end

-- ============================================================
-- BUILD FLAT ITEM LIST
-- ============================================================

local function build_items(parsed)
    local items={}
    for _, sec in ipairs(SEC_ORDER) do
        local chs=parsed[sec] or {}
        if #chs>0 then
            items[#items+1]={type="header",sec=sec,label=SEC_LABELS[sec],count=#chs,color=COL[sec]}
            for _, ch in ipairs(chs) do
                items[#items+1]={type="channel",sec=sec,ch=ch,color=COL[sec],id=0}
            end
        end
    end
    for i,item in ipairs(items) do
        if item.type=="channel" then item.id=i end
    end
    return items
end

-- ============================================================
-- TRACK CREATION
-- ============================================================

-- stereo_mode: "split" = stereo channels become two mono tracks (Name L / Name R);
--              "stereo" = one track with a stereo hardware input pair.
local function create_tracks(items, sel, stereo_mode)
    local proj=0
    local hw=0  -- 0-based hardware input counter

    reaper.Undo_BeginBlock()
    reaper.PreventUIRefresh(1)

    local function add_track(nm, color, recinput, chans)
        local idx=reaper.CountTracks(proj)
        reaper.InsertTrackAtIndex(idx,true)
        local tr=reaper.GetTrack(proj,idx)
        reaper.GetSetMediaTrackInfo_String(tr,"P_NAME",nm,true)
        reaper.SetTrackColor(tr,color)
        reaper.SetMediaTrackInfo_Value(tr,"I_RECINPUT",recinput)
        if chans then reaper.SetMediaTrackInfo_Value(tr,"I_NCHAN",chans) end
    end

    for _,item in ipairs(items) do
        if item.type=="channel" and sel[item.id] then
            local ch     = item.ch
            local stereo = ch.number:sub(-1)=="s"
            local color  = reaper.ColorToNative(item.color.r,item.color.g,item.color.b)
            if stereo and stereo_mode=="stereo" then
                -- stereo input pair encoded as 1024 + left input index
                add_track(ch.name, color, 1024+hw, 2)
                hw=hw+2
            elseif stereo then
                add_track(ch.name.." L", color, hw); hw=hw+1
                add_track(ch.name.." R", color, hw); hw=hw+1
            else
                add_track(ch.name, color, hw); hw=hw+1
            end
        end
    end

    reaper.TrackList_AdjustWindows(false)
    reaper.UpdateArrange()
    reaper.PreventUIRefresh(-1)
    reaper.Undo_EndBlock("DiGiCo: Import channels",-1)
end

-- ============================================================
-- GFX UI
-- ============================================================

local WIN_W  = 540
local WIN_H  = 640
local HDR_H  = 98
local FTR_H  = 56
local ROW_H  = 22
local LIST_H = WIN_H - HDR_H - FTR_H
local VISIBLE = math.floor(LIST_H / ROW_H)

local function gc(r,g,b,a)
    gfx.r=r/255; gfx.g=g/255; gfx.b=b/255; gfx.a=(a or 255)/255
end

local function fill(x,y,w,h,r,g,b)
    gc(r,g,b); gfx.rect(x,y,w,h,1)
end

local function draw_btn(x,y,w,h,label,hover,active)
    if active and hover then fill(x,y,w,h,0,110,215)
    elseif active       then fill(x,y,w,h,0, 85,200)
    elseif hover        then fill(x,y,w,h,72, 72, 77)
    else                     fill(x,y,w,h,50, 50, 54) end
    gc(170,170,175); gfx.rect(x,y,w,h,0)
    gfx.setfont(1,"Arial",12)
    local tw,th = gfx.measurestr(label)
    gc(220,220,226)
    gfx.x=x+math.floor((w-tw)/2)
    gfx.y=y+math.floor((h-th)/2)
    gfx.drawstr(label)
end

local function run_ui(items, filename)
    local sel={}
    for i,item in ipairs(items) do
        if item.type=="channel" then sel[i]=not item.ch.is_default end
    end

    local scroll    = 0
    local max_scroll= math.max(0,#items-VISIBLE)
    local stereo_mode = "split"   -- "split" = L/R mono tracks, "stereo" = one stereo track

    local function count_sel()
        local n=0
        for i,item in ipairs(items) do
            if item.type=="channel" and sel[i] then n=n+1 end
        end
        return n
    end

    gfx.init("DiGiCo to Reaper",WIN_W,WIN_H,0)
    gfx.setfont(1,"Arial",13)

    local prev_cap=0

    local function frame()
        local char=gfx.getchar()
        if char==-1 then return end           -- window closed by OS
        if char==27  then gfx.quit(); return end  -- Escape
        if char==13  then                         -- Return / Enter → import
            local n=count_sel()
            if n>0 then gfx.quit(); create_tracks(items,sel,stereo_mode); return end
        end
        if char==115 or char==83 then             -- S → toggle stereo mode
            stereo_mode = (stereo_mode=="split") and "stereo" or "split"
        end

        local mx,my  = gfx.mouse_x, gfx.mouse_y
        local cap    = gfx.mouse_cap
        local clicked= (cap&1)==1 and (prev_cap&1)==0

        -- Mouse wheel
        if gfx.mouse_wheel~=0 then
            local dir = gfx.mouse_wheel>0 and -3 or 3
            scroll=math.max(0,math.min(scroll+dir,max_scroll))
            gfx.mouse_wheel=0
        end

        -- ── Background ──────────────────────────────────────────
        fill(0,0,WIN_W,WIN_H, 28,28,30)

        -- ── Header ──────────────────────────────────────────────
        fill(0,0,WIN_W,HDR_H, 40,40,44)
        gc(58,58,62); gfx.rect(0,HDR_H-1,WIN_W,1,1)

        gfx.setfont(1,"Arial",15,string.byte("b"))
        gc(240,240,245); gfx.x,gfx.y=16,12
        gfx.drawstr("DiGiCo to Reaper")

        gfx.setfont(1,"Arial",11)
        gc(140,140,148); gfx.x,gfx.y=16,34
        gfx.drawstr(filename)

        -- Toolbar
        local tby,tbh,tbw=60,26,108
        local tbs={{"Select All",14},{"Deselect All",128},{"Remove Unnamed",242}}
        for _,tb in ipairs(tbs) do
            local lbl,bx=tb[1],tb[2]
            local hov=mx>=bx and mx<bx+tbw and my>=tby and my<tby+tbh
            draw_btn(bx,tby,tbw,tbh,lbl,hov,false)
            if clicked and hov then
                if lbl=="Select All" then
                    for i,it in ipairs(items) do if it.type=="channel" then sel[i]=true  end end
                elseif lbl=="Deselect All" then
                    for i,it in ipairs(items) do if it.type=="channel" then sel[i]=false end end
                else
                    for i,it in ipairs(items) do
                        if it.type=="channel" and it.ch.is_default then sel[i]=false end
                    end
                end
            end
        end

        -- Stereo mode toggle
        local st_x,st_w=356,170
        local st_lbl=(stereo_mode=="split") and "Stereo: Split to L/R" or "Stereo: Keep Stereo"
        local st_hov=mx>=st_x and mx<st_x+st_w and my>=tby and my<tby+tbh
        draw_btn(st_x,tby,st_w,tbh,st_lbl,st_hov,stereo_mode=="stereo")
        if clicked and st_hov then
            stereo_mode = (stereo_mode=="split") and "stereo" or "split"
        end

        -- ── Channel list ─────────────────────────────────────────
        local list_y0=HDR_H

        for vis=0,VISIBLE-1 do
            local idx=scroll+vis+1
            if idx>#items then break end
            local item=items[idx]
            local ry=list_y0+vis*ROW_H
            local cr,cg,cb=item.color.r,item.color.g,item.color.b

            if item.type=="header" then
                fill(0,ry,WIN_W,ROW_H, math.floor(cr*.18),math.floor(cg*.18),math.floor(cb*.18))
                gfx.setfont(1,"Arial",11,string.byte("b"))
                gc(cr,cg,cb)
                gfx.x,gfx.y=12,ry+5
                gfx.drawstr(item.label.."   ("..item.count..")")
            else
                -- Row background
                local row_hov=mx>=0 and mx<WIN_W-6 and my>=ry and my<ry+ROW_H
                if row_hov then
                    fill(0,ry,WIN_W-6,ROW_H, 52,52,56)
                elseif vis%2==0 then
                    fill(0,ry,WIN_W-6,ROW_H, 33,33,36)
                end

                -- Checkbox
                local is_sel=sel[idx] or false
                local cx,cy=12,ry+5
                fill(cx,cy,12,12, 62,62,67)
                if is_sel then
                    fill(cx+2,cy+2,8,8, cr,cg,cb)
                end

                -- Number
                gfx.setfont(1,"Arial",10)
                gc(108,108,116)
                gfx.x,gfx.y=32,ry+6
                gfx.drawstr(item.ch.number)

                -- Name
                gfx.setfont(1,"Arial",13)
                if item.ch.is_default then gc(78,78,84) else gc(210,210,218) end
                gfx.x,gfx.y=108,ry+4
                gfx.drawstr(item.ch.name)

                if clicked and row_hov then sel[idx]=not is_sel end
            end
        end

        -- Scrollbar
        if #items>VISIBLE then
            local sbx=WIN_W-5
            fill(sbx,list_y0,5,LIST_H, 38,38,42)
            local th=math.max(20,math.floor(LIST_H*VISIBLE/#items))
            local ty=list_y0+math.floor((LIST_H-th)*scroll/math.max(1,max_scroll))
            fill(sbx,ty,5,th, 100,100,108)
            -- Drag scrollbar
            if (cap&1)==1 and mx>=sbx then
                local new=math.floor((my-list_y0-th/2)/math.max(1,LIST_H-th)*max_scroll+.5)
                scroll=math.max(0,math.min(new,max_scroll))
            end
        end

        -- ── Footer ───────────────────────────────────────────────
        local ftr_y=WIN_H-FTR_H
        fill(0,ftr_y,WIN_W,FTR_H, 40,40,44)
        gc(58,58,62); gfx.rect(0,ftr_y,WIN_W,1,1)

        local n_sel=count_sel()
        local imp_lbl="Import "..n_sel.." Track"..(n_sel~=1 and "s" or "")

        -- Keyboard hint
        gfx.setfont(1,"Arial",11)
        gc(90,90,96)
        local hint="↵ Return to import  ·  S stereo mode  ·  Esc to cancel"
        local hw=gfx.measurestr(hint)
        gfx.x=math.floor((WIN_W-208-hw)/2); gfx.y=ftr_y+20
        gfx.drawstr(hint)

        -- Cancel button
        local can_x,can_y,can_w,can_h=WIN_W-298,ftr_y+12,82,32
        local can_hov=mx>=can_x and mx<can_x+can_w and my>=can_y and my<can_y+can_h
        draw_btn(can_x,can_y,can_w,can_h,"Cancel",can_hov,false)
        if clicked and can_hov then gfx.quit(); return end

        -- Import button
        local imp_x,imp_y,imp_w,imp_h=WIN_W-208,ftr_y+12,200,32
        local can_import=n_sel>0
        local imp_hov=can_import and mx>=imp_x and mx<imp_x+imp_w and my>=imp_y and my<imp_y+imp_h
        draw_btn(imp_x,imp_y,imp_w,imp_h,imp_lbl,imp_hov,can_import)
        if clicked and imp_hov then
            gfx.quit()
            create_tracks(items,sel,stereo_mode)
            return
        end

        prev_cap=cap
        gfx.update()
        reaper.defer(frame)
    end

    reaper.defer(frame)
end

-- ============================================================
-- MAIN
-- ============================================================

local ok,filepath=reaper.GetUserFileNameForRead("","Select DiGiCo Show File (.ses or .rtf)","ses")
if not ok then return end

local is_ses=filepath:lower():match("%.ses$")~=nil
local f,err=io.open(filepath,is_ses and "rb" or "r")
if not f then
    reaper.ShowMessageBox("Could not open file:\n"..tostring(err),"DiGiCo to Reaper",0)
    return
end
local content=f:read("*all"); f:close()

if not content or content=="" then
    reaper.ShowMessageBox("File is empty.","DiGiCo to Reaper",0)
    return
end

local parsed
if is_ses then
    parsed=parse_ses(content)
    if not parsed then
        reaper.ShowMessageBox("Not a valid DiGiCo SD show file (.ses).","DiGiCo to Reaper",0)
        return
    end
else
    parsed=parse_rtf(content)
end

local total=0
for _,sec in ipairs(SEC_ORDER) do total=total+#(parsed[sec] or {}) end
if total==0 then
    reaper.ShowMessageBox("No channels found in this file.","DiGiCo to Reaper",0)
    return
end

local filename=filepath:match("([^/\\]+)$") or filepath
run_ui(build_items(parsed), filename)
