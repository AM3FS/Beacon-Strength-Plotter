VK3FS Signal Strength plotter for radio beacons

Select your radio and its COM port in the Radio connection panel.
Icom presets fill the default CI-V address. For a Yaesu FTX-1 or FT-891,
choose the Enhanced USB CAT COM port. The FTX-1 monitors Main via CAT-1
with one stop bit. On the FT-891, disable CAT RTS and select VFO mode
before tuning presets. Yaesu plots show raw CAT counts from 0 to 255.
Match the baud rate configured on the radio, then click Connect.
The COM port list refreshes when you open the dropdown or click Refresh ports.
On Windows it includes virtual ports exposed as COM names, such as COM21.
Use the client end of a virtual pair when connecting through a CAT sharing
server. You can also type a COM port name into the field.
The first launch requires a manual connection. With Radio auto-connect
enabled in Setup, later launches try the last working radio immediately and
retry every five seconds for up to one minute. A valid S-meter reply stops
the retries. If the radio is still unavailable, click Connect to try again.
An unexpected COM port failure after a verified connection starts a new
retry window. Retained readings stay on the plot, with a gap during the
outage. After reconnection, live sampling resumes. Click Disconnect
during an attempt to stop the retry sequence.

Presets: IC-905 (AC), IC-9700 (A2), IC-705 (A4), IC-7300 (94),
IC-7300MK2 (B6), IC-7610 (98), IC-7760 (B2), and Custom Icom.
Custom Icom is for compatible radios with the same CI-V commands.

The graph shows a rolling window from five minutes to 24 hours. It keeps
samples in memory for the current connection, and missing replies leave gaps.
Zoom with the slider, the plus and minus buttons, or the mouse wheel
over the plot. Drag the horizontal scrollbar below the plot to review earlier readings
at the current zoom level when the recorded history is longer than the
visible window. Logging
continues while you review history. Click Live to follow the newest readings.
The X axis shows local 24-hour time (HH:MM). Clock ticks are five minutes
apart on short views, ten minutes on the one-hour view, and hourly on the
24-hour view. The intervening views use 15-, 30- or 60-minute ticks.
The IC-9700 axis uses bench readings and Icom's reference points. Other named
profiles use approximate S-units interpolated from the common raw references
0, 120 and 241. Custom Icom uses the raw axis. The live number is always raw.
The live raw CI-V reading is at the bottom right. The frequency readout
supports both five- and six-byte CI-V replies. It uses radio-style
grouping at 10 Hz display resolution, such as 144.436.00.

In Setup, choose a Sample interval of 0.5 seconds or any whole second from 1 to 10.
Changes take effect during the current session and are saved for next time.
The Radio connection dot is lime while the radio is responding and red
when it is disconnected, waiting for its first reply or stops responding.

The app remembers a COM port for each radio model after receiving a valid
S-meter reply. Choose a model to recall its last working port. Failed
connection attempts leave that port intact. The selected radio, baud,
and address are saved when the window closes. The plot starts at five minutes. Existing settings
carry over and the old COM port is used as an initial suggestion.

Run Start Plotter.bat to start from source with Python and Tkinter.
Run Build EXE.bat on Windows to build dist\Icom S-meter Plotter.exe.
The build requires 64-bit Python with Tcl/Tk, and installs dependencies.
The finished EXE runs without a separate Python installation.

Click Screenshot to copy an image of the plotter window to the Windows
clipboard. Press Ctrl+V in an app that accepts pasted images.


Pause and resume
Click Pause to suspend S-meter sampling and freeze the plot's live edge.
Choose Clear Plot to discard the current readings after confirmation.
If sampling is running, new readings begin a fresh trace.
Click Resume to continue the same session. Existing readings stay in the
rolling history, and the pause appears as a gap. Zoom and scrolling are
available while paused.

Beacon presets
The list is sorted alphabetically by callsign. An exact match to the
reported radio frequency appears automatically in the Beacon dropdown.
The selection clears when the frequency is unknown or has no saved match.
Connect the radio, then select a saved beacon to set its frequency and
CW / Filter 1 on Icom, or CW on Yaesu. The app checks frequency and mode readback.
Callsign and location are labels. The initial VK5RSE preset uses the
example supplied for this build: Mount Gambier, 145.550 MHz.

Select Add New Beacon to enter a callsign, location and frequency in MHz.
Use Manage Beacons to edit or delete saved entries. Delete All asks for
confirmation before clearing the saved beacon list. Changes are saved
immediately. Adding, editing and deleting entries do not tune the radio.
The Manage Beacons window opens to fit the saved list where screen space
allows. Resize it or use the scrollbars to see longer entries and lists.
The Beacon dropdown, Manage Beacons and VKSpotter results use matching row
colours for Australian VHF, UHF and microwave bands. Other frequencies use
the standard background.

On first launch, enter your callsign and Maidenhead locator or choose Skip.
Under Setup > Your station, enter your callsign and a 6- or 8-character
Maidenhead locator (for example, QF22AA or QF22AA12). Both fields are optional
and saved locally when you select OK. The plotter calculates the grid cell's
centre coordinates for distance, bearing and mapping.

The country or territory is estimated offline from the grid cell centre
using Natural Earth 1:50m Admin 0 Countries public-domain map data.
Coastlines and borders are approximate, and a cell centred outside the
mapped land areas displays Unknown.
Source: https://www.naturalearthdata.com/downloads/50m-cultural-vectors/50m-admin-0-countries-2/

In Setup, choose Day, Night or Auto under Display to preview it immediately.
OK saves your choice and Cancel restores the previous display setting. Auto uses the computer's local
time, switching to Day at 07:00 and Night at 18:00. The app saves your choice. The app background shows a sunny forest in Day mode and a moonlit
version in Night mode. The graph defaults to 35% transparency.

The main heading and window title start with your callsign when set in Setup.
The top-right clocks show local time and UTC in 24-hour format.


Plot controls
In Setup, choose Signal strength or dBm to switch the Y axis. The dBm scale is
estimated from CI-V meter values, with a fixed selectable S9 reference
of -73 dBm for HF or -93 dBm for VHF/UHF (default). Select the reference
that fits the measurements you are viewing. It applies to the whole trace.
Open Setup to adjust plot transparency from solid (0%) to clear (100%).
The chart previews the slider as you drag. OK saves the changes and Cancel
restores the previous appearance. The grid, labels and trace stay opaque.
Warn on close and Radio auto-connect are enabled by default. Auto-connect
uses the last verified radio, COM port, baud rate and CI-V address.

Use Setup to choose the smoothing level, including 50, 75 and 100 samples.
The moving average applies to the displayed trace and restarts after gaps.

In Setup, choose VKSpotter, enter your username with its exact capitalisation
and your password, then select Sign In & Fetch. Direct lookup uses Microsoft Edge
in a separate browser session. Amber shows a saved login or a sign-in in
progress. Lime means VKSpotter confirmed sign-in for the fetch. Red means
there is no confirmed sign-in and no attempt is in progress. The plotter
saves a confirmed login in Windows Credential Manager even if the beacon
table cannot be read. The username and password fields are greyed out when
a verified login is available. A failed sign-in re-enables both fields.
Choose Forget Saved Login to remove the login and enter different details.
If the page cannot be read directly, choose Open VKSpotter, sign in in your
browser, use Copy above the Beacons table, then choose Paste Beacon List.
Search by callsign, grid, location, frequency or status. Select a record and
use Use Selected to add it to your saved beacons, or to refresh
the details of an existing callsign and frequency. Importing does not tune
the radio. The list displays VKSpotter's reported status as retrieved.

When adding or editing one beacon, enter its callsign and choose Look Up on
VKSpotter. If a login is saved, the lookup fetches the list automatically.
A unique callsign match fills the Maidenhead grid and reported status,
including the report date if available. If several frequencies are listed,
Ctrl+click or Shift+click to select the ones you want, then choose Use Selected.
Choose Add All to import all displayed entries with readable
frequencies. A multi-row import saves each frequency as a separate beacon.
Selecting one row in the beacon editor lets you review the details before
selecting OK to save it.
If VKSpotter supplies the grid or status without a readable frequency,
enter the frequency in MHz in the editor before saving.

Distance and initial true heading use the centres of your station grid
and the beacon grid. Beacon records can use 4-, 6- or 8-character grids.
Shorter grids give coarser estimates. Enter your station grid in Setup and
add or edit a beacon's grid in Manage Beacons. The Beacon box displays the
selected beacon's distance and true heading even with the radio disconnected.
The readout stays blank if either grid is missing or invalid.

Select a beacon and click Map beside Plot files to open the terrain map and
ground-height profile for the direct radio path. This uses the centre of each
Maidenhead grid, just as the distance and heading readout does. On first use,
enter a Maps JavaScript API key from a Google Cloud project with billing
enabled. Restrict the key to the website http://127.0.0.1:8767/* and allow
Maps JavaScript API. For the elevation profile, also enable Maps Elevation API
in the same project and allow it on the key. Change or clear the key in Setup
using Maps key. Keep the plotter open while loading the map.
If you do not have a key, choose Export KML in the Map dialogue. Open
Google My Maps and import the saved KML into a map layer.

Use the Y zoom slider on the right edge of the plot to expand weak signals.
Move it upwards for more magnification or down to 1 for the full scale.

In Setup > Synoptic chart, tick Auto fetch BoM and click OK. The program fills
in the current chart URL and checks again when you open the Synoptic popup.
Untick Auto fetch BoM to switch back to your own image URL.

Hepburn Chart appears beside Synoptic. To select your forecast, go to
Setup > Hepburn Chart, click Choose a chart, then copy the URL for the
forecast you want and paste it into the Forecast URL field. The toolbar
button opens the saved forecast in your default browser. If the URL is
blank, the button offers to open https://www.dxinfocentre.com/tropo.html.

Plot files > Save plot saves the retained readings to a timestamped .bplot
file. Disconnect before opening a saved plot for zooming and scrolling.
Export CSV writes the original CI-V readings with UTC timestamps. Blank
readings indicate gaps. Smoothing never changes either export.

Mute radio sets the selected receiver's AF volume to zero while plotting
continues. Unmute radio can restore the earlier volume during the same
connection. Disconnecting or closing does not change the radio's volume.
