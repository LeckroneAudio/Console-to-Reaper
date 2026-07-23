Console to Reaper v3.5
======================

INSTALLATION:
1. Drag "Console to Reaper.app" to your Applications folder
2. Double-click to launch
3. Look for the 🎛️ icon in your menu bar (top-right of screen)

SUPPORTED CONSOLES:
- DiGiCo SD/Quantum — export a Session Report (.rtf) from the console
  (make sure to include Channels when printing the report), or copy the
  .ses show file directly, or pull channels live over OSC
- DiGiCo S-Series — copy the .session file from the console or S-Series software
- Yamaha Rivage PM — export a show file (.RIVAGEPM) from Rivage PM Manager
- Yamaha DM7 — copy the .dm7f project file from the console
- Allen & Heath dLive / Avantis — export a show file (.tar.gz) from dLive/Avantis Director
- Allen & Heath SQ — export a scene from SQ-MixPad or save to USB (.dat)
- Behringer X32 / Midas M32 — save a scene from the console or X32-Edit/M32-Edit (.scn)
- Behringer Wing — save a snapshot from the console or Wing-Edit (.snap)
- Avid S6L / VENUE — save a show file from the console or VENUE software (.dsh)
  NOT FULLY SUPPORTED: Input and Aux/Bus channels only. Group and Matrix
  outputs are not extracted from S6L show files.
- Waves LV1 — save a session file from LV1 software (.emo)

USAGE:
1. Export a show file from your console (see Supported Consoles above)
2. Click the 🎛️ menu bar icon and select "Open Converter"
3. Your browser will open automatically to the converter
4. Upload your show file using the upload area, or add channels manually
5. Select/deselect channels and organize as needed
6. Choose your stereo mode and click Download
7. Import into Reaper:
   - Track → Insert tracks from template → Select file
   - OR drag the .RTrackTemplate file directly into Reaper

SESSIONS (TABS):
The converter supports multiple sessions open at once using tabs.
- Click "+" to add a new session tab
- Double-click a tab name to rename it
- Click the × on a tab to close it
- Each tab is fully independent — channels, colors, and selections do not carry over between tabs

UPLOADING FILES:
- Drag and drop a show file onto the upload area, or click to browse
- Supported formats: .rtf (DiGiCo), .RIVAGEPM (Yamaha Rivage PM), .tar.gz (A&H dLive)
- To load a new file into an existing session, click "Upload New File" — you will be prompted before the session is cleared
- If no file is loaded, you can still build a session by adding channels manually

ADDING AND EDITING CHANNELS:
- Click "+ Add Channel Manually" to add channels that aren't in your session report
- Choose the channel type (Input, Aux, Group, Matrix), name, quantity, and whether it's stereo
- Double-click any channel name to rename it inline
- Use the Up/Down arrow keys while renaming to move to the next or previous channel
- Click the ✕ on any channel row to remove it immediately
- Select one or more channels and press Delete (or Backspace on Windows) to remove with confirmation

SELECTING CHANNELS:
- Click any channel row to highlight it
- Cmd/Ctrl+Click to toggle individual channels on/off
- Shift+Click to select a range
- Cmd/Ctrl+A to select all
- Click empty space in the list to deselect all

REORDERING CHANNELS:
- Drag any channel row to reorder it
- Drag a highlighted channel to move all highlighted channels together as a group
- The list will scroll automatically when dragging near the top or bottom

QUICK SELECTIONS:
Each section (Inputs, Aux Outputs, Group Outputs, Matrix Outputs) has a checkbox to quickly include or exclude the entire section from the export.

CHANNEL COLORS:
- Click the color dot on any channel row to assign a color that will appear on the track in Reaper
- Right-click the dot (or click ✕) to clear the color
- Click the color dot next to a section label to apply one color to every channel in that section
- When multiple channels are highlighted, setting or clearing a color applies to all highlighted channels at once
- When multiple channels are highlighted, a bulk toolbar appears to apply or clear colors across all selected channels at once
- Stereo channels (split into L/R) will both receive the same color
- Section colors reset to default when a new file is loaded

STEREO MODE:
Use the stereo toggle to choose how stereo channels are exported:
- Split into Mono: Each stereo channel becomes two separate mono tracks (L and R)
- Keep as Stereo: Each stereo channel becomes one stereo track

REAPER TRACK SETTINGS:
All exported tracks are:
- Record armed and ready to receive input
- Routed sequentially to hardware inputs (mono channels take one input, stereo channels take two)
- Colored according to any colors you assigned

EXPORT OPTIONS:
- Download .RTrackTemplate — imports directly into Reaper as a track template
- Export as CSV — saves the channel list as a .csv file for use in spreadsheets or other tools

UNDO:
- Click the Undo button (or use your browser's standard undo if available) to step back through changes
- Up to 50 undo steps are stored per session

DARK MODE:
Click the "Dark Mode" button in the top-right corner to switch the interface to a dark background.

MENU BAR OPTIONS:
- Open Converter — opens the converter in your default browser
- Restart Server — restarts the local server if something goes wrong
- Quit — properly closes the app and stops the server

LUA SCRIPTS (OPTIONAL — REAPER DIRECT IMPORT):
ReaScripts are included in the .zip for importing directly into Reaper
without using the converter app or browser at all — one per console, plus
DiGiCo has extra options.

  DiGiCo_to_Reaper_Full.lua       — DiGiCo SD/Quantum (.ses or .rtf), all
                                     sections (Inputs/Aux/Groups/Matrix)
                                     with an interactive channel selector
  DiGiCo_to_Reaper.lua            — DiGiCo SD/Quantum (.rtf), Input Channels only
  DiGiCo_OSC_to_Reaper.lua        — DiGiCo SD/Quantum, live import over OSC
  DiGiCo_OSC_Settings.lua         — configures the OSC connection used above
  DiGiCo_S_Series_to_Reaper.lua   — DiGiCo S-Series (.session), Input Channels only
  Rivage_to_Reaper.lua            — Yamaha Rivage PM (.RIVAGEPM), Input Channels only
  DM7_to_Reaper.lua               — Yamaha DM7 (.dm7f), Input Channels only
  dLive_to_Reaper.lua             — A&H dLive/Avantis (.tar.gz), Input Channels only
  SQ_to_Reaper.lua                — A&H SQ (.dat), Input Channels only
  M32_to_Reaper.lua               — Behringer X32 / Midas M32 (.scn), Input Channels only
  Wing_to_Reaper.lua              — Behringer Wing (.snap), Input Channels only
  S6L_to_Reaper.lua               — Avid S6L / VENUE (.dsh), Input Channels only
  LV1_to_Reaper.lua               — Waves LV1 (.emo), Input Channels only

What the single-console scripts do (all except DiGiCo_to_Reaper_Full.lua):
- Prompt you to select the appropriate show file
- Parse all Input Channels from the file
- Create one track per channel (stereo channels split into L/R mono tracks)
- Assign sequential hardware inputs
- Color tracks by console

Note: These scripts only import Input Channels. They do not support Aux,
Group, or Matrix outputs, custom colors, stereo-keep mode, or CSV export.
For full control over any session (or for DiGiCo Aux/Group/Matrix), use
DiGiCo_to_Reaper_Full.lua or the converter app instead.

Note: Every script except DiGiCo_to_Reaper.lua, DiGiCo_to_Reaper_Full.lua,
DiGiCo_OSC_to_Reaper.lua, and M32_to_Reaper.lua requires Python 3 to be
installed on your system (included automatically if you have the converter app).

SETTING UP THE LUA SCRIPTS IN REAPER:
1. In Reaper, go to Actions > Load ReaScript
2. Browse to and select the .lua file for your console
3. Reaper will load the script and add it to your Actions list
4. To run it: Actions > Action List, find the script, click Run
5. (Optional) To assign a keyboard shortcut:
   - Find the script in the Action List
   - Click "Add shortcut" and press your preferred key combination
     (e.g. Cmd+Shift+I for DiGiCo, Cmd+Shift+R for Rivage, Cmd+Shift+D for dLive)

TROUBLESHOOTING:
- If the browser doesn't open automatically, click "Open Converter" from the menu bar icon
- The port number is shown in the menu bar tooltip (usually :8081)
- If the app won't open on first launch, right-click → Open to bypass Gatekeeper
- If the browser shows a disconnect message, the app has been closed — relaunch from Applications

CHANNEL TYPES:
- Inputs — input channels from the console
- Aux Outputs — aux/monitor mixes
- Group Outputs — group/subgroup buses
- Matrix Outputs — matrix outputs
- Custom — channels added manually

Built by: Michael Leckrone
Contact: leckroneaudio@gmail.com
Version: 3.5
GitHub: github.com/LeckroneAudio/Console-to-Reaper
