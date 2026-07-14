-- DiGiCo OSC to Reaper — Connection Settings
-- Edits the saved console IP/ports used by DiGiCo_OSC_to_Reaper.lua.
-- The import action itself never prompts once settings are saved; run this
-- when the console IP or ports change.

local function trim(s) return (s:gsub("^%s+", ""):gsub("%s+$", "")) end

local ip = reaper.GetExtState("DiGiCo_OSC", "ip")
local sp = reaper.GetExtState("DiGiCo_OSC", "sendport")
local lp = reaper.GetExtState("DiGiCo_OSC", "listenport")
if ip == "" then ip = "192.168.10.232" end
if sp == "" then sp = "8012" end
if lp == "" then lp = "8011" end

local ok, ret = reaper.GetUserInputs(
    "DiGiCo OSC Settings", 3,
    "Console IP,Console Rcv port,Console Send port,extrawidth=100",
    ip .. "," .. sp .. "," .. lp)
if not ok then return end

local parts = {}
for p in ret:gmatch("[^,]+") do parts[#parts + 1] = trim(p) end
ip, sp, lp = parts[1] or ip, parts[2] or sp, parts[3] or lp

reaper.SetExtState("DiGiCo_OSC", "ip", ip, true)
reaper.SetExtState("DiGiCo_OSC", "sendport", sp, true)
reaper.SetExtState("DiGiCo_OSC", "listenport", lp, true)

reaper.ShowMessageBox(
    "Saved.\n\nConsole: " .. ip .. "\nConsole Rcv port: " .. sp ..
    "\nConsole Send port: " .. lp,
    "DiGiCo OSC Settings", 0)
