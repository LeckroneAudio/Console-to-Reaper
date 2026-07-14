-- DiGiCo OSC to Reaper — Live Console Importer
-- Fetches channel names directly from a DiGiCo SD/Quantum console over OSC
-- (External Control) and shows an interactive channel selector before
-- creating tracks. No file export needed.
--
-- Console setup (Setup > External Control):
--   Enable External Control: YES
--   Add device: type iPad/OSC, IP = this computer,
--   Send port = the "listen" port below, Receive port = the "send to" port.
--
-- Requires python3 on this computer (macOS: built in / xcode tools).
--
-- Installation:
--   1. Actions > Load ReaScript, select this file
--   2. Assign a shortcut in the Action List if desired

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
-- OSC FETCH (embedded Python helper — ReaScript has no UDP)
-- ============================================================

-- Protocol (verified on Quantum 338 v2242, External Control type iPad):
--   query  = OSC address + "/?" suffix, NO arguments
--   NEVER send a bare address or a string arg — those are SETTERS.
--   /Console/Channels/?            -> per-section channel counts
--   /Console/<Section>/modes/?     -> int array, 1 = mono, 2 = stereo
--   /Console/Name/?  /Console/Session/Filename/?  -> labels for the UI
--   names: /Input_Channels/{n}/Channel_Input/name/?
--          /{Aux|Group|Matrix}_Outputs/{n}/Buss_Trim/name/?
--   iPad set replies have no prefix; the generic OSC set replies with an
--   /sd prefix and answers names only (no counts/modes) — both accepted.

local PYFETCH = [==[
import socket, struct, sys, time, re
ip, sp, lp = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
def pad(b): return b + b'\x00' * ((4 - len(b) % 4) % 4)
def q(a): return pad(a.encode() + b'\x00') + pad(b',\x00')
def parse(d):
    try:
        if d[:1] != b'/': return None
        e = d.index(b'\x00'); addr = d[:e].decode()
        i = (e + 4) & ~3
        if i >= len(d) or d[i:i+1] != b',': return addr, []
        te = d.index(b'\x00', i); tags = d[i+1:te].decode(); i = (te + 4) & ~3
        args = []
        for t in tags:
            if t == 's':
                se = d.index(b'\x00', i)
                args.append(d[i:se].decode('ascii', 'replace'))
                i = (se + 4) & ~3
            elif t in 'if':
                args.append(struct.unpack('>' + t, d[i:i+4])[0]); i += 4
            else:
                return addr, args
        return addr, args
    except Exception:
        return None
SECS = [('inputs', 'Input_Channels', 'Channel_Input/name', 'Ch', 128),
        ('aux', 'Aux_Outputs', 'Buss_Trim/name', 'Aux', 48),
        ('groups', 'Group_Outputs', 'Buss_Trim/name', 'Grp', 24),
        ('matrix', 'Matrix_Outputs', 'Buss_Trim/name', 'Matrix', 24)]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('0.0.0.0', lp)); s.settimeout(0.05)
dest = (ip, sp)
def collect(max_wait, sink, done=None, idle=0.25):
    dl = time.time() + max_wait
    last_rx = None
    while time.time() < dl:
        if done and done(): break
        if last_rx is not None and (time.time() - last_rx) > idle: break
        try:
            d, _a = s.recvfrom(65536)
        except socket.timeout:
            continue
        p = parse(d)
        if p:
            sink(p[0], p[1])
            last_rx = time.time()
info = {'counts': {}, 'modes': {}, 'console': '', 'session': ''}
def sink1(addr, args):
    if addr == '/Console/Name' and args: info['console'] = str(args[0])
    elif addr == '/Console/Session/Filename' and args: info['session'] = str(args[0])
    else:
        m = re.match(r'^/Console/(\w+)/modes$', addr)
        if m:
            info['modes'][m.group(1)] = [int(v) for v in args]; return
        m = re.match(r'^/Console/(\w+)$', addr)
        if m and args:
            try: info['counts'][m.group(1)] = int(args[0])
            except Exception: pass
for qq in ['/Console/Name', '/Console/Session/Filename', '/Console/Channels',
           '/Console/Input_Channels/modes', '/Console/Aux_Outputs/modes',
           '/Console/Group_Outputs/modes', '/Console/Matrix_Outputs/modes']:
    s.sendto(q(qq + '/?'), dest); time.sleep(0.01)
collect(1.2, sink1, done=lambda: (len(info['counts']) >= 4 and
    len(info['modes']) >= 3 and info['console'] and info['session']))
pend = {}; qs = {}
for k, sec, leaf, dp, mx in SECS:
    cnt = min(info['counts'].get(sec, mx), mx)
    for n in range(1, cnt + 1):
        w = '/%s/%d/%s' % (sec, n, leaf)
        pend[w] = (k, n); qs[w] = q(w + '/?')
names = {}
def sink2(addr, args):
    a = addr[3:] if addr.startswith('/sd/') else addr
    if a in pend and args and isinstance(args[0], str):
        names[pend.pop(a)] = args[0]
for _ in range(3):
    if not pend: break
    for w, pkt in qs.items():
        if w in pend:
            s.sendto(pkt, dest); time.sleep(0.002)
    collect(1.2, sink2, done=lambda: not pend, idle=0.35)
print('meta\t0\t%s\t0\t0' % info['console'])
print('meta\t1\t%s\t0\t0' % info['session'])
for k, sec, leaf, dp, mx in SECS:
    r = re.compile('^%s \\d+$' % dp)
    modes = info['modes'].get(sec, [])
    ns = [n for (kk, n) in names if kk == k]
    cnt = max(ns) if ns else 0
    for n in range(1, cnt + 1):
        nm = names.get((k, n))
        if nm is None: continue
        nm = nm.rstrip()
        d1 = 1 if (not nm or r.match(nm)) else 0
        st = 1 if (n <= len(modes) and modes[n-1] == 2) else 0
        if not nm: nm = '%s %d' % (dp, n)
        print('%s\t%d\t%s\t%d\t%d' % (k, n, nm, d1, st))
]==]

local function trim2(s) return (s:gsub("^%s+", ""):gsub("%s+$", "")) end

-- Show the connection dialog. Returns ip, sp, lp or nil if cancelled.
local function ask_settings(ip, sp, lp)
    local ok, ret = reaper.GetUserInputs(
        "DiGiCo OSC Import", 3,
        "Console IP,Console Rcv port,Console Send port,extrawidth=100",
        ip .. "," .. sp .. "," .. lp)
    if not ok then return nil end
    local parts = {}
    for p in ret:gmatch("[^,]+") do parts[#parts + 1] = trim2(p) end
    return parts[1] or ip, parts[2] or sp, parts[3] or lp
end

-- Run the embedded fetcher. Returns raw TSV output ("" = no response),
-- or nil on a local problem that was already reported to the user.
local function run_fetch(ip, sp, lp)
    local tmp = os.tmpname() .. ".py"
    local f = io.open(tmp, "w")
    if not f then
        reaper.ShowMessageBox("Could not write temp file.", "DiGiCo OSC", 0)
        return nil
    end
    f:write(PYFETCH); f:close()

    -- Find a Python interpreter. Prefer the one bundled inside the
    -- Console/DiGiCo to Reaper app (py2app ships a full Python3), so app
    -- users need nothing extra; fall back to python3 on the PATH.
    local py_prefix = nil
    for _, app in ipairs({
        "/Applications/DiGiCo to Reaper.app",
        "/Applications/Console to Reaper.app",
    }) do
        local stub = app .. "/Contents/MacOS/python"
        local fh = io.open(stub, "rb")
        if fh then
            fh:close()
            py_prefix = string.format(
                'DYLD_LIBRARY_PATH="%s/Contents/Frameworks/Python3.framework" ' ..
                'PYTHONHOME="%s/Contents/Resources" "%s"', app, app, stub)
            break
        end
    end
    if not py_prefix then
        local chk = io.popen('command -v python3 2>/dev/null', "r")
        local py = chk and chk:read("*l") or nil
        if chk then chk:close() end
        if py and py ~= "" then
            py_prefix = "python3"
        end
    end
    if not py_prefix then
        os.remove(tmp)
        reaper.ShowMessageBox(
            "No Python interpreter found.\n\n" ..
            "Either install the Console to Reaper app, or install the\n" ..
            "Xcode Command Line Tools by running this in Terminal:\n\n" ..
            "    xcode-select --install",
            "DiGiCo OSC", 0)
        return nil
    end

    local cmd = string.format('%s "%s" %s %s %s 2>/dev/null', py_prefix, tmp, ip, sp, lp)
    local ph = io.popen(cmd, "r")
    local out = ph and ph:read("*all") or nil
    if ph then ph:close() end
    os.remove(tmp)
    return out or ""
end

local function fetch_from_console()
    local ip = reaper.GetExtState("DiGiCo_OSC", "ip")
    local sp = reaper.GetExtState("DiGiCo_OSC", "sendport")
    local lp = reaper.GetExtState("DiGiCo_OSC", "listenport")
    local have_saved = (ip ~= "" and sp ~= "" and lp ~= "")
    if ip == "" then ip = "192.168.10.232" end
    if sp == "" then sp = "8012" end
    if lp == "" then lp = "8011" end

    -- Only prompt when there are no saved settings; otherwise go straight
    -- to the fetch so the action runs hotkey -> picker with no dialogs.
    if not have_saved then
        ip, sp, lp = ask_settings(ip, sp, lp)
        if not ip then return nil end
    end

    local out = run_fetch(ip, sp, lp)
    if out == "" and have_saved then
        -- Saved settings didn't answer (console IP changed?) — offer the
        -- dialog once and retry.
        ip, sp, lp = ask_settings(ip, sp, lp)
        if not ip then return nil end
        out = run_fetch(ip, sp, lp)
    end
    if out == nil then return nil end

    if out == "" then
        reaper.ShowMessageBox(
            "No response from the console at " .. ip .. ".\n\n" ..
            "Check:\n" ..
            "- Setup > External Control is enabled on the console\n" ..
            "- A device entry points at this computer's IP\n" ..
            "- Ports match (console Rcv = " .. sp .. ", console Send = " .. lp .. ")\n" ..
            "- Nothing else (Companion/bridge) is bound to port " .. lp,
            "DiGiCo OSC", 0)
        return nil
    end

    reaper.SetExtState("DiGiCo_OSC", "ip", ip, true)
    reaper.SetExtState("DiGiCo_OSC", "sendport", sp, true)
    reaper.SetExtState("DiGiCo_OSC", "listenport", lp, true)

    local parsed = {inputs = {}, aux = {}, groups = {}, matrix = {}}
    local pfix = {inputs = "CH", aux = "AUX", groups = "GRP", matrix = "MTX"}
    local console_name, session_name = "", ""
    for line in out:gmatch("[^\n]+") do
        local sec, num, name, isdef, stereo =
            line:match("^(%w+)\t(%d+)\t(.-)\t([01])\t([01])$")
        if sec == "meta" then
            if num == "0" then console_name = name else session_name = name end
        elseif sec and parsed[sec] then
            parsed[sec][#parsed[sec] + 1] = {
                number = pfix[sec] .. num .. (stereo == "1" and "s" or ""),
                name = name, type = sec, is_default = (isdef == "1"),
            }
        end
    end

    local src = ip
    if console_name ~= "" then src = console_name end
    if session_name ~= "" then src = src .. " — " .. session_name end
    return parsed, src
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

local parsed, src = fetch_from_console()
if not parsed then return end

local total = #parsed.inputs + #parsed.aux + #parsed.groups + #parsed.matrix
if total == 0 then
    reaper.ShowMessageBox(
        "Connected but no channels came back.\n\nIs a session loaded on the console?",
        "DiGiCo OSC", 0)
    return
end

run_ui(build_items(parsed), "Console: " .. src)
