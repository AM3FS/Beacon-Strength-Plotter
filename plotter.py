"""Standalone receive S-meter history for Icom CI-V radios."""
from collections import deque
import csv
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from io import BytesIO
import json
import os
import re
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
import time
import tkinter as tk
from tkinter import filedialog, font, messagebox, simpledialog, ttk
from urllib.parse import urlsplit
from urllib.request import urlopen
import webbrowser

from beacon_map import MAP_ORIGIN, MY_MAPS_URL, BeaconMapServer, map_page, write_path_kml
from station_location import beacon_path, country_at, maidenhead_coordinates
from updates import APP_VERSION, check_latest_release
from vkspotter import BEACONS_URL, clipboard_beacons, fetch_beacons, listed_frequency_hz
from vkspotter_credentials import forget_login, read_login, save_login

RADIO_ADDRESSES = {
    'IC-905': 'AC',
    'IC-9700': 'A2',
    'IC-705': 'A4',
    'IC-7300': '94',
    'IC-7300MK2': 'B6',
    'IC-7610': '98',
    'IC-7760': 'B2',
    'Custom Icom': '',
}
DEFAULT_RADIO = 'IC-9700'
DEFAULT_SYNOPTIC_CHART = 'https://reg.bom.gov.au/fwo/IDY00030.202611211800.png'
GOOGLE_CLOUD_CONSOLE_URL = 'https://console.cloud.google.com/'
HEPBURN_CHART_INDEX = 'https://www.dxinfocentre.com/tropo.html'
ADD_BEACON = 'Add New Beacon'
DEFAULT_BEACONS = ({'callsign': 'VK5RSE', 'location': 'Mount Gambier', 'hz': 145_550_000},)
SAMPLE_INTERVALS = (0.5, *range(1, 11))
TIME_WINDOWS = (
    (300, '5 min', 5), (600, '10 min', 5), (900, '15 min', 5), (1800, '30 min', 5),
    (3600, '1 hr', 10), (7200, '2 hr', 15), (14400, '4 hr', 30), (28800, '8 hr', 60),
    (43200, '12 hr', 60), (86400, '24 hr', 60),
)
HISTORY_SECONDS = TIME_WINDOWS[-1][0]
PLOT_BOTTOM_MARGIN = 34
PLOT_STAGGERED_BOTTOM_MARGIN = 44
Y_ZOOM_TOP = 14
MIN_WINDOW_WIDTH = 780
MIN_WINDOW_HEIGHT = 470


def validate_chart_url(source):
    parsed = urlsplit(source)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Enter a complete HTTP or HTTPS URL.')


def read_synoptic_chart(source, size):
    from PIL import Image

    max_bytes = 5 * 1024 * 1024
    validate_chart_url(source)
    with urlopen(source, timeout=12) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('The chart image is too large.')
    with Image.open(BytesIO(data), formats=('PNG', 'JPEG', 'GIF', 'WEBP', 'BMP')) as image:
        image.thumbnail(size, Image.Resampling.LANCZOS)
        return image.copy()


def clock_ticks(end, span, interval):
    start = end - span
    first = -int(-start // 60) * 60
    for timestamp in range(first, int(end) + 1, 60):
        local = datetime.fromtimestamp(timestamp)
        if local.minute % interval == 0:
            yield timestamp, local.strftime('%H:%M')



def settings_file():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'IC9700 S-meter Plotter' / 'settings.json'


def load_settings(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    temporary.replace(path)


DISPLAY_THEMES = {
    'Day': dict(background='#edf1f5', panel='#ffffff', control='#dce5ed',
                text='#192b3c', muted='#536477', border='#aebdca',
                hover='#cbdbe9', selection='#23629d', grid='#d2dce5', reference='#8398ad'),
    'Night': dict(background='#101923', panel='#121e2a', control='#263747',
                  text='#d6e2e9', muted='#91a5b7', border='#475d71',
                  hover='#344e64', selection='#285b88', grid='#304253', reference='#517484'),
}

# Australian VHF–SHF band ranges, with backgrounds for Day and Night.
BEACON_BANDS = {
    '6m': (((50, 54),), '#f6eac8', '#453922'),
    '2m': (((144, 148),), '#dbeaf8', '#29445b'),
    '70cm': (((430, 450),), '#daf0df', '#284838'),
    '23cm': (((1240, 1300),), '#e8def5', '#433653'),
    '13cm': (((2300, 2302), (2400, 2450)), '#f8e3d6', '#4b382d'),
    '9cm': (((3300, 3400),), '#d8efeb', '#24494a'),
    '6cm': (((5650, 5850),), '#f6dfea', '#4c3549'),
    '3cm': (((10000, 10450),), '#e5edcf', '#3b442a'),
}


def beacon_band(hz):
    if hz is None:
        return None
    for band, (ranges, _, _) in BEACON_BANDS.items():
        if any(low * 1_000_000 <= hz < high * 1_000_000 for low, high in ranges):
            return band
    return None


def beacon_row_background(hz, theme):
    band = beacon_band(hz)
    if band is None:
        return DISPLAY_THEMES[theme]['panel']
    _, day, night = BEACON_BANDS[band]
    return night if theme == 'Night' else day


DISPLAY_MODES = ('Day', 'Night', 'Auto')
PLOTTER_HEADING = 'Beacon Strength Plotter'


def effective_theme(mode, hour):
    if mode == 'Auto':
        return 'Day' if 7 <= hour < 18 else 'Night'
    return mode


def clock_labels(now):
    return f"Local  {now.astimezone():%H:%M:%S}", f"UTC  {now.astimezone(timezone.utc):%H:%M:%S}"


def signal_colour(raw, theme='Night'):
    weak, orange, strong = (
        ((25, 65, 135), (166, 76, 0), (195, 32, 50)) if theme == 'Day' else
        ((59, 203, 239), (255, 153, 44), (245, 54, 66))
    )
    if raw <= 120:
        start, end, fraction = weak, orange, raw / 120
    else:
        start, end, fraction = orange, strong, min(1, (raw - 120) / 80)
    channels = (round(low + (high - low) * fraction) for low, high in zip(start, end))
    return '#' + ''.join(f'{channel:02x}' for channel in channels)


SIGNAL_COLOURS = {theme: tuple(signal_colour(raw, theme) for raw in range(256)) for theme in DISPLAY_THEMES}


# S-unit positions are displayed six dB apart. Below S9 the raw breakpoints
# use the bench readings; above S9 the scale uses the IC-9700 manual endpoints.
METER_ANCHORS = ((0, 0), (20, 12), (62, 30), (67, 36), (120, 54), (241, 114))
METER_TICKS = ((0, 'S0'), (12, 'S2'), (24, 'S4'), (36, 'S6'), (48, 'S8'),
               (54, 'S9'), (74, '+20 dB'), (94, '+40 dB'), (114, '+60 dB'))


AXIS_UNITS = ('Signal strength', 'dBm')
DBM_REFERENCES = {'HF (−73 dBm)': -73, 'VHF/UHF (−93 dBm)': -93}
S9_REFERENCE_URL = 'https://vienna.iaru-r1.org/wp-content/uploads/2019/01/VHF_Handbook_V8.02a.pdf#page=68'
DEFAULT_DBM_REFERENCE = 'VHF/UHF (−93 dBm)'


def dbm_ticks(reference):
    return tuple((position, f'{reference + position - 54} dBm') for position, _ in METER_TICKS)


SMOOTHING_WINDOWS = {'Off': 1, 'Light (3 samples)': 3, 'Medium (5 samples)': 5,
                     'Strong (10 samples)': 10, 'Extra strong (20 samples)': 20,
                     '50 samples': 50, '75 samples': 75, '100 samples': 100}
PLOT_FORMAT = 'vk3fs-beacon-plot-v1'


def smooth_samples(samples, count):
    if count == 1:
        yield from samples
        return
    recent = deque(maxlen=count)
    total = 0
    for timestamp, raw in samples:
        if raw is None:
            recent.clear()
            total = 0
            yield timestamp, None
        else:
            if len(recent) == count:
                total -= recent[0]
            recent.append(raw)
            total += raw
            yield timestamp, round(total / len(recent))


def read_plot(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('format') != PLOT_FORMAT:
        raise ValueError('Choose a plot file saved by Beacon Strength Plotter.')
    model = data.get('radio_model')
    samples = data.get('samples')
    if not isinstance(model, str) or model not in RADIO_ADDRESSES or not isinstance(samples, list) or not samples:
        raise ValueError('The plot file is missing its radio model or readings.')
    history = deque()
    previous = 0
    for point in samples:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError('The plot file contains an invalid reading.')
        stamp, raw = point
        if (type(stamp) not in (int, float) or not previous <= stamp < 253402214400
                or (raw is not None and (type(raw) is not int or not 0 <= raw <= 255))):
            raise ValueError('The plot file contains an invalid timestamp or CI-V reading.')
        history.append((stamp, raw))
        previous = stamp
    return model, history


def write_plot_csv(path, samples):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(('timestamp_utc', 'raw_ci_v'))
        for timestamp, raw in samples:
            writer.writerow((datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec='milliseconds'), raw))


class FirstRunStationDialog(simpledialog.Dialog):
    def body(self, frame):
        padding = {'padx': 8, 'pady': 5}
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text='Enter your station details to calculate beacon distances and bearings.',
                  wraplength=310).grid(row=0, column=0, columnspan=2, sticky='w', **padding)
        ttk.Label(frame, text='Callsign').grid(row=1, column=0, sticky='w', **padding)
        self.callsign = ttk.Entry(frame, width=18)
        self.callsign.grid(row=1, column=1, sticky='ew', **padding)
        ttk.Label(frame, text='Maidenhead locator').grid(row=2, column=0, sticky='w', **padding)
        self.locator = ttk.Entry(frame, width=18)
        self.locator.grid(row=2, column=1, sticky='ew', **padding)
        ttk.Label(frame, text='Use 6 or 8 characters, e.g. QF22AA.').grid(
            row=3, column=1, sticky='w', padx=8)
        return self.callsign

    def buttonbox(self):
        buttons = ttk.Frame(self, padding=8)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='Save', command=self.ok, default='active').pack(side='right')
        ttk.Button(buttons, text='Skip', command=self.cancel).pack(side='right', padx=6)
        self.bind('<Return>', self.ok)
        self.bind('<Escape>', self.cancel)

    def validate(self):
        callsign = self.callsign.get().strip().upper()
        if not callsign:
            messagebox.showinfo('Your station', 'Enter your callsign or choose Skip.', parent=self)
            self.callsign.focus_set()
            return False
        locator = self.locator.get().strip().upper()
        try:
            coordinates = maidenhead_coordinates(locator)
        except ValueError as exc:
            messagebox.showwarning('Maidenhead locator', str(exc), parent=self)
            self.locator.focus_set()
            return False
        self.result = callsign, locator, coordinates
        return True


class MapKeyDialog(simpledialog.Dialog):
    def __init__(self, parent, key, *, opening_map=False):
        self.key = key
        self.opening_map = opening_map
        super().__init__(parent, 'Google Maps')

    def body(self, frame):
        background = ttk.Style(self).lookup('TFrame', 'background')
        self.configure(background=background)
        frame.configure(background=background)
        ttk.Label(frame, text='Maps JavaScript API key').pack(anchor='w', padx=12, pady=(8, 0))
        self.entry = ttk.Entry(frame, width=44, show='•', exportselection=False)
        self.entry.insert(0, self.key)
        self.entry.pack(fill='x', padx=12, pady=(5, 10))
        self.console_font = font.nametofont('TkDefaultFont', root=self).copy()
        self.console_font.configure(underline=True)
        link_colour = '#8cd7ff' if background == DISPLAY_THEMES['Night']['background'] else '#125b91'
        console_link = ttk.Label(frame, text='Open Google Cloud Console', cursor='hand2', takefocus=True,
                                 font=self.console_font, foreground=link_colour)
        console_link.pack(anchor='w', padx=12, pady=(0, 6))
        for sequence in ('<Button-1>', '<Return>', '<space>'):
            console_link.bind(sequence, self.open_console)
        steps = (
            'Create or select a Google Cloud project and link an active billing account.',
            'Enable Maps JavaScript API. For the terrain profile, also enable Maps Elevation API.',
            'Under APIs & Services → Credentials, create an API key. Allow Maps JavaScript API '
            'and, if needed, Maps Elevation API on that key.',
            f'Under Website restrictions, add {MAP_ORIGIN}/*. Save, then paste the key above.',
        )
        for index, step in enumerate(steps, start=1):
            ttk.Label(frame, text=f'{index}. {step}', justify='left', wraplength=380).pack(
                anchor='w', padx=12, pady=(0, 5))
        ttk.Label(frame, text='For more help, see the Beacon Strength Plotter guide when available '
                              'or ask an AI assistant. Keep your API key private.',
                  justify='left', wraplength=380).pack(anchor='w', padx=12, pady=(5, 0))
        return self.entry

    def open_console(self, _event):
        webbrowser.open(GOOGLE_CLOUD_CONSOLE_URL)
        return 'break'

    def buttonbox(self):
        buttons = ttk.Frame(self, padding=(17, 6, 17, 12))
        buttons.pack(fill='x')
        if self.opening_map:
            ttk.Button(buttons, text='Export KML…', command=self.export_kml).pack(side='left')
        ttk.Button(buttons, text='Cancel', command=self.cancel).pack(side='right')
        ttk.Button(buttons, text='Open Map' if self.opening_map else 'Save', command=self.ok).pack(
            side='right', padx=6)
        self.bind('<Return>', self.ok)
        self.bind('<Escape>', self.cancel)

    def validate(self):
        key = self.entry.get().strip()
        if self.opening_map and not key:
            messagebox.showinfo('Google Maps', 'Enter a Maps JavaScript API key or choose Export KML.',
                                parent=self)
            return False
        self.result = ('key', key)
        return True

    def export_kml(self):
        self.result = ('export', '')
        self.cancel()


class SetupDialog(simpledialog.Dialog):
    def __init__(self, parent):
        self.percent = tk.IntVar(parent, value=parent.plot_transparency)
        self.warn = tk.BooleanVar(parent, value=parent.warn_on_close)
        self.auto_connect = tk.BooleanVar(parent, value=parent.auto_connect)
        self.axis_units = tk.StringVar(parent, value=parent.axis_units.get())
        self.dbm_reference = tk.StringVar(parent, value=parent.dbm_reference.get())
        self.display_mode = tk.StringVar(parent, value=parent.display_theme.get())
        self.smoothing = tk.StringVar(parent, value=parent.smoothing.get())
        self.sample_interval = tk.StringVar(parent, value=f'{parent.sample_interval:g} s')
        self.callsign = tk.StringVar(parent, value=parent.station_callsign)
        self.locator = tk.StringVar(parent, value=parent.station_locator)
        self.synoptic_source = tk.StringVar(parent, value=parent.synoptic_source)
        self.hepburn_source = tk.StringVar(parent, value=parent.hepburn_source)
        self.country_label = tk.StringVar(parent)
        self.update_check_job = None
        super().__init__(parent, 'Setup')

    def body(self, frame):
        self.parent.setup_dialog = self
        self.body_frame = frame
        station = ttk.LabelFrame(frame, text='Your station', padding=8)
        station.pack(fill='x', padx=10, pady=(8, 0))
        station.columnconfigure(1, weight=1)
        ttk.Label(station, text='Callsign').grid(row=0, column=0, sticky='w', padx=(0, 8))
        ttk.Entry(station, textvariable=self.callsign, width=18).grid(row=0, column=1, sticky='w')
        ttk.Label(station, text='Maidenhead locator').grid(row=1, column=0, sticky='w', pady=4)
        self.locator_entry = ttk.Entry(station, textvariable=self.locator, width=18)
        self.locator_entry.grid(row=1, column=1, sticky='w')
        ttk.Label(station, text='Country or territory').grid(row=2, column=0, sticky='nw', padx=(0, 8))
        ttk.Label(station, textvariable=self.country_label, wraplength=190).grid(row=2, column=1, sticky='w')
        self.locator_trace = self.locator.trace_add('write', self.update_country)
        self.update_country()

        display = ttk.LabelFrame(frame, text='Display', padding=8)
        display.pack(fill='x', padx=10, pady=(8, 0))
        for mode in DISPLAY_MODES:
            ttk.Radiobutton(display, text=mode, value=mode, variable=self.display_mode,
                            command=self.preview_display).pack(
                side='left', padx=(0, 8),
            )

        axis = ttk.LabelFrame(frame, text='Y-axis units', padding=8)
        axis.pack(fill='x', padx=10, pady=(8, 0))
        choices = ttk.Frame(axis)
        choices.pack(fill='x')
        for units in AXIS_UNITS:
            ttk.Radiobutton(choices, text=units, value=units, variable=self.axis_units,
                            command=self.update_reference_state).pack(side='left', padx=(0, 8))
        reference = ttk.Frame(axis)
        reference.pack(fill='x', pady=(5, 0))
        ttk.Label(reference, text='S9 reference').pack(side='left', padx=(0, 6))
        self.reference_picker = ttk.Combobox(reference, textvariable=self.dbm_reference,
                                             values=tuple(DBM_REFERENCES), width=19)
        self.reference_picker.pack(side='left')
        self.link_font = font.nametofont('TkDefaultFont', root=self).copy()
        self.link_font.configure(underline=True)
        self.reference_link = ttk.Label(reference, text='Learn more', cursor='hand2',
                                        takefocus=True, font=self.link_font)
        self.reference_link.pack(side='left', padx=(8, 0))
        for sequence in ('<Button-1>', '<Return>', '<space>'):
            self.reference_link.bind(sequence, self.open_s9_reference)
        self.update_reference_state()
        smoothing = ttk.Frame(axis)
        smoothing.pack(fill='x', pady=(5, 0))
        ttk.Label(smoothing, text='Smoothing').pack(side='left', padx=(0, 6))
        ttk.Combobox(smoothing, textvariable=self.smoothing, values=tuple(SMOOTHING_WINDOWS),
                     width=25, state='readonly').pack(side='left')

        sampling = ttk.Frame(frame)
        sampling.pack(fill='x', padx=10, pady=(8, 0))
        ttk.Label(sampling, text='Sample interval').pack(side='left', padx=(0, 6))
        ttk.Combobox(sampling, textvariable=self.sample_interval, state='readonly', width=7,
                     values=tuple(f'{seconds:g} s' for seconds in SAMPLE_INTERVALS)).pack(side='left')

        ttk.Label(frame, text='Plot transparency').pack(anchor='w', padx=10, pady=(8, 0))
        ttk.Label(frame, text='0% = solid   ·   100% = clear').pack(anchor='w', padx=10)
        self.transparency_slider = tk.Scale(frame, from_=0, to=100, orient='horizontal', length=310,
                                           variable=self.percent, command=self.preview, highlightthickness=0)
        self.transparency_slider.pack(padx=10, pady=(0, 8))
        ttk.Checkbutton(frame, text='Warn on close', variable=self.warn).pack(anchor='w', padx=10)
        options = ttk.Frame(frame)
        options.pack(fill='x', padx=10, pady=(4, 8))
        ttk.Checkbutton(options, text='Auto-connect to radio', variable=self.auto_connect).pack(side='left')
        self.update_button = ttk.Button(options, text='Check for updates…', command=self.check_for_updates)
        self.update_button.pack(side='right')
        synoptic = ttk.LabelFrame(frame, text='Synoptic chart', padding=8)
        synoptic.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Label(synoptic, text='Image URL').pack(anchor='w', pady=(0, 4))
        self.synoptic_entry = ttk.Entry(synoptic, textvariable=self.synoptic_source, width=34)
        self.synoptic_entry.pack(fill='x')

        hepburn = ttk.LabelFrame(frame, text='Hepburn Chart', padding=8)
        hepburn.pack(fill='x', padx=10, pady=(0, 8))
        hepburn_header = ttk.Frame(hepburn)
        hepburn_header.pack(fill='x', pady=(0, 4))
        ttk.Label(hepburn_header, text='Forecast URL').pack(side='left')
        self.hepburn_link = ttk.Label(hepburn_header, text='Choose a chart', cursor='hand2',
                                      takefocus=True, font=self.link_font)
        self.hepburn_link.pack(side='right')
        for sequence in ('<Button-1>', '<Return>', '<space>'):
            self.hepburn_link.bind(sequence, self.open_hepburn_index)
        self.hepburn_entry = ttk.Entry(hepburn, textvariable=self.hepburn_source, width=34)
        self.hepburn_entry.pack(fill='x')
        self.apply_palette()
        return self.transparency_slider

    def buttonbox(self):
        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.pack(fill='x')
        ttk.Button(buttons, text='VKSpotter…', command=self.open_vkspotter).pack(side='left')
        ttk.Button(buttons, text='Maps key…', command=self.setup_map_key).pack(side='left', padx=5)
        ttk.Button(buttons, text='Cancel', command=self.cancel).pack(side='right')
        ttk.Button(buttons, text='OK', command=self.ok).pack(side='right', padx=(0, 8))
        self.bind('<Return>', self.ok)
        self.bind('<Escape>', self.cancel)

    def setup_map_key(self):
        try:
            result = MapKeyDialog(self, self.parent.google_maps_key).result
            if result:
                self.parent.google_maps_key = result[1]
                self.parent.persist_settings()
        finally:
            self.grab_set()

    def open_vkspotter(self):
        try:
            coordinates = maidenhead_coordinates(self.locator.get())
        except ValueError:
            coordinates = None
        try:
            result = VKSpotterDialog(self, coordinates, self.parent.active_theme).result
            if result:
                self.parent.import_vkspotter_beacons(result if isinstance(result, list) else [result])
        finally:
            self.grab_set()

    def apply_palette(self):
        palette = DISPLAY_THEMES[self.parent.active_theme]
        self.configure(background=palette['background'])
        self.body_frame.configure(background=palette['background'])
        link_colour = '#8cd7ff' if self.parent.active_theme == 'Night' else '#125b91'
        self.reference_link.configure(foreground=link_colour)
        self.hepburn_link.configure(foreground=link_colour)
        self.transparency_slider.configure(background=palette['background'], foreground=palette['text'],
                                           troughcolor=palette['panel'], activebackground=palette['hover'])

    def preview_display(self):
        self.parent.display_mode_preview = self.display_mode.get()
        self.parent.apply_display_theme()

    def update_reference_state(self):
        self.reference_picker.configure(state='readonly' if self.axis_units.get() == 'dBm' else 'disabled')

    def open_s9_reference(self, _event):
        webbrowser.open(S9_REFERENCE_URL)
        return 'break'

    def open_hepburn_index(self, _event):
        webbrowser.open(HEPBURN_CHART_INDEX)
        return 'break'

    def check_for_updates(self):
        result = Queue(maxsize=1)

        def check():
            try:
                result.put(check_latest_release())
            except (OSError, KeyError, TypeError, ValueError) as exc:
                result.put(exc)

        def show_result():
            self.update_check_job = None
            try:
                release = result.get_nowait()
            except Empty:
                self.update_check_job = self.after(100, show_result)
                return
            self.update_button.configure(text='Check for updates…', state='normal')
            if isinstance(release, Exception):
                messagebox.showwarning('Check for updates', f'Could not check GitHub.\n\n{release}', parent=self)
            elif release is None:
                messagebox.showinfo('Check for updates', f'Version {APP_VERSION} is up to date.', parent=self)
            else:
                version, installer_url = release
                if messagebox.askyesno(
                    'Update available',
                    f'{version} is available. Download the Windows installer?\n\n'
                    'Save any plots you want to keep and close the plotter before running it.',
                    parent=self,
                ):
                    if not webbrowser.open(installer_url):
                        messagebox.showwarning('Check for updates', 'Could not open the default browser.', parent=self)
            if self.winfo_exists():
                self.grab_set()

        self.update_button.configure(text='Checking…', state='disabled')
        Thread(target=check, daemon=True).start()
        self.update_check_job = self.after(100, show_result)

    def preview(self, _value):
        self.parent.plot_transparency_preview = self.percent.get()
        self.parent.render_plot_wallpaper()

    def update_country(self, *_):
        locator = self.locator.get().strip()
        if not locator:
            label = 'Enter a 6- or 8-character locator'
        else:
            try:
                coordinates = maidenhead_coordinates(locator)
            except ValueError:
                label = 'Enter a valid locator'
            else:
                try:
                    label = country_at(*coordinates) or 'Unknown (no land match)'
                except (OSError, ValueError):
                    label = 'Country data unavailable'
        self.country_label.set(label)

    def validate(self):
        locator = self.locator.get().strip()
        try:
            self.station_coordinates = maidenhead_coordinates(locator) if locator else None
        except ValueError as exc:
            messagebox.showwarning('Maidenhead locator', str(exc), parent=self)
            self.locator_entry.focus_set()
            return False
        for title, variable, entry in (
            ('Synoptic chart', self.synoptic_source, self.synoptic_entry),
            ('Hepburn Chart', self.hepburn_source, self.hepburn_entry),
        ):
            try:
                if source := variable.get().strip():
                    validate_chart_url(source)
            except ValueError as exc:
                messagebox.showwarning(title, str(exc), parent=self)
                entry.focus_set()
                return False
        return True

    def apply(self):
        self.result = (self.percent.get(), self.warn.get(), self.auto_connect.get(),
                       self.axis_units.get(), self.dbm_reference.get(), self.display_mode.get(), self.smoothing.get(),
                       self.callsign.get().strip().upper(), self.locator.get().strip().upper(),
                       self.station_coordinates, float(self.sample_interval.get().split()[0]),
                       self.synoptic_source.get().strip(), self.hepburn_source.get().strip())

    def destroy(self):
        self.locator.trace_remove('write', self.locator_trace)
        if self.update_check_job is not None:
            self.after_cancel(self.update_check_job)
        super().destroy()


def meter_position(raw, anchors):
    for (low_raw, low_pos), (high_raw, high_pos) in zip(anchors, anchors[1:]):
        if raw <= high_raw:
            return low_pos + (raw - low_raw) * (high_pos - low_pos) / (high_raw - low_raw)
    return anchors[-1][1]


IC9700_POSITIONS = tuple(meter_position(raw, METER_ANCHORS) for raw in range(256))
STANDARD_ANCHORS = ((0, 0), (120, 54), (241, 114))
STANDARD_POSITIONS = tuple(meter_position(raw, STANDARD_ANCHORS) for raw in range(256))
RAW_POSITIONS = tuple(range(256))
RAW_TICKS = tuple((raw, str(raw)) for raw in (0, 60, 120, 180, 255))


def zoomed_meter_ticks(ceiling, *, raw, reference):
    if raw or reference is not None:
        step = next(step for step in (1, 2, 3, 5, 10, 20, 30, 60) if step >= ceiling / 6)
    else:
        step = 6 if ceiling <= 42 else 12
    if raw:
        return tuple((position, str(position)) for position in range(0, int(ceiling) + 1, step))
    if reference is not None:
        return tuple((position, f'{reference + position - 54} dBm')
                     for position in range(0, int(ceiling) + 1, step))
    return tuple((position, f'S{position // 6}' if position <= 54 else f'+{position - 54} dB')
                 for position in range(0, int(ceiling) + 1, step))


def visible_trace_segment(start, end, top):
    x1, y1 = start[:2]
    x2, y2 = end[:2]
    if y1 < top and y2 < top:
        return None
    if y1 < top or y2 < top:
        crossing = x1 + (x2 - x1) * (top - y1) / (y2 - y1)
        if y1 < top:
            x1, y1 = crossing, top
        else:
            x2, y2 = crossing, top
    return x1, y1, x2, y2


def radio_setup(settings):
    model = settings.get('model', DEFAULT_RADIO)
    if not isinstance(model, str) or model not in RADIO_ADDRESSES:
        model = DEFAULT_RADIO
    return model, settings.get('address', RADIO_ADDRESSES[model])



def startup_radio(settings, model, ports):
    saved = settings.get('last_connected_radio')
    if saved is None and ports.get(model):
        # Earlier versions only saved a per-model port after a valid meter reply.
        saved = {'model': model, 'port': ports[model], 'baud': settings.get('baud', '115200'),
                 'address': settings.get('address', RADIO_ADDRESSES[model])}
    if not isinstance(saved, dict):
        return None
    try:
        if saved['model'] not in RADIO_ADDRESSES or not isinstance(saved['port'], str) or not saved['port'].strip():
            return None
        if int(saved['baud']) <= 0 or not 1 <= int(saved['address'], 16) < 0xE0:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return saved


def beacon_frequency_hz(frequency):
    match = re.fullmatch(r'([0-9]{1,6})(?:[.]([0-9]{1,6}))?(?: *MHz)?', frequency.strip(), re.IGNORECASE)
    if not match:
        raise ValueError('Enter frequency in MHz with up to six decimal places, e.g. 145.550.')
    whole, fraction = match.groups()
    hz = int(whole) * 1_000_000 + int((fraction or '').ljust(6, '0'))
    if not hz:
        raise ValueError('Frequency must be greater than zero.')
    return hz


def beacon_record(callsign, location, frequency):
    callsign = callsign.strip().upper()
    location = location.strip()
    if not callsign or not location:
        raise ValueError('Enter the beacon callsign and location.')
    return {'callsign': callsign, 'location': location, 'hz': beacon_frequency_hz(frequency)}


def beacon_callsign_key(callsign):
    return callsign.strip().upper().removesuffix('/B')


def beacon_identity(beacon):
    return beacon_callsign_key(beacon['callsign']), beacon['hz']


def vkspotter_matches(records, callsign):
    return [record for record in records if beacon_callsign_key(record['callsign']) == callsign]


def format_radio_frequency(hz):
    if hz is None:
        return 'Frequency —'
    mhz, remainder = divmod(hz, 1_000_000)
    khz, hz_remainder = divmod(remainder, 1_000)
    return f'{mhz}.{khz:03d}.{hz_remainder // 10:02d}'


def beacon_label(beacon):
    return f"{beacon['callsign']}  ·  {beacon['location']}  ·  {beacon['hz'] / 1e6:.6f} MHz"


def format_beacon_path(origin, locator, *, labelled=False):
    if origin is None or not isinstance(locator, str):
        return ''
    try:
        destination = maidenhead_coordinates(locator, allow_four=True)
    except ValueError:
        return ''
    distance, bearing = beacon_path(origin, destination)
    if labelled:
        heading = f'  Bearing: {round(bearing) % 360:03d}°' if bearing is not None else ''
        return f'Distance: {distance:,.0f} km{heading}'
    heading = f' · {round(bearing) % 360:03d}° true' if bearing is not None else ''
    return f'{distance:,.0f} km{heading}'


class VKSpotterDialog(simpledialog.Dialog):
    def __init__(self, parent, coordinates, theme, *, lookup=None):
        self.coordinates = coordinates
        self.theme = theme
        self.lookup = lookup
        self.records = []
        self.matches = []
        self.poll_job = None
        self.saved_login = None
        self.verified_login = None
        try:
            self.saved_login = read_login()
        except (OSError, ValueError):
            pass
        super().__init__(parent, 'VKSpotter Beacons')

    def body(self, frame):
        palette = DISPLAY_THEMES[self.theme]
        self.configure(background=palette['background'])
        frame.configure(background=palette['background'])
        frame.columnconfigure(2, weight=1)
        frame.rowconfigure(3, weight=1)
        self.dialog_body = frame
        ttk.Label(frame, text='Username').grid(row=0, column=0, sticky='w', padx=(10, 8))
        self.username = ttk.Entry(frame, width=20)
        if self.saved_login:
            self.username.insert(0, self.saved_login[0])
        self.username.grid(row=0, column=1, sticky='w', pady=4)
        ttk.Label(frame, text='Password').grid(row=1, column=0, sticky='w', padx=(10, 8))
        self.password = ttk.Entry(frame, width=20, show='•', exportselection=False)
        self.password.grid(row=1, column=1, sticky='w', pady=4)
        controls = ttk.Frame(frame)
        controls.grid(row=0, column=2, columnspan=2, rowspan=2, sticky='e', padx=10)
        self.fetch = ttk.Button(controls, text='Sign In & Fetch', command=self.start_fetch)
        self.fetch.pack(side='left')
        ttk.Button(controls, text='Open VKSpotter', command=lambda: webbrowser.open(BEACONS_URL)).pack(
            side='left', padx=6)
        self.paste = ttk.Button(controls, text='Paste Beacon List', command=self.paste_beacons)
        self.paste.pack(side='left')
        ttk.Label(frame, text='Search').grid(row=2, column=0, sticky='w', padx=(10, 8))
        self.search = ttk.Entry(frame)
        if self.lookup:
            self.search.insert(0, self.lookup)
        self.search.grid(row=2, column=1, columnspan=3, sticky='ew', padx=(0, 10), pady=(10, 6))
        self.search.bind('<KeyRelease>', self.refresh_rows)
        columns = ('Callsign', 'Grid', 'Location', 'MHz', 'Status', 'Distance / Heading')
        table_style = 'Beacon.Treeview'
        style = ttk.Style(self)
        style.configure(table_style, background=palette['panel'], fieldbackground=palette['panel'],
                        foreground=palette['text'])
        style.map(table_style, background=[('selected', palette['selection'])],
                  foreground=[('selected', '#ffffff')])
        self.table = ttk.Treeview(frame, columns=columns, show='headings', selectmode='extended',
                                  height=11, style=table_style)
        for band, (_, day, night) in BEACON_BANDS.items():
            self.table.tag_configure(band, background=night if self.theme == 'Night' else day)
        self.table.grid(row=3, column=0, columnspan=3, sticky='nsew', padx=(10, 0), pady=4)
        for column, width in zip(columns, (95, 70, 195, 90, 80, 145)):
            self.table.heading(column, text=column)
            self.table.column(column, width=width, minwidth=40)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.table.yview)
        scroll.grid(row=3, column=3, sticky='ns', padx=(0, 10), pady=4)
        self.table.configure(yscrollcommand=scroll.set)
        initial_note = ('Choose Sign In & Fetch to load the beacon list.' if self.saved_login else
                        'Sign in to fetch beacons, or paste the list from VKSpotter’s Copy button.')
        self.note = tk.StringVar(self, value=initial_note)
        sign_in = ttk.Frame(frame)
        sign_in.grid(row=4, column=0, columnspan=4, sticky='w', padx=10, pady=(6, 0))
        self.sign_in_dot = tk.Canvas(sign_in, width=14, height=14, highlightthickness=0)
        self.sign_in_dot.configure(background=ttk.Style(self).lookup('TFrame', 'background'))
        self.sign_in_dot.pack(side='left', padx=(0, 4))
        self.sign_in_text = ttk.Label(sign_in)
        self.sign_in_text.pack(side='left')
        self.show_sign_in(('pending' if self.lookup else 'saved') if self.saved_login else 'out')
        ttk.Label(frame, textvariable=self.note, wraplength=630).grid(
            row=5, column=0, columnspan=4, sticky='w', padx=10, pady=(3, 4))
        self.lock_credentials(bool(self.saved_login))
        return self.fetch if self.saved_login else self.username

    def buttonbox(self):
        self.dialog_body.pack_configure(fill='both', expand=True)
        buttons = ttk.Frame(self, padding=(15, 6, 15, 12))
        buttons.pack(fill='x')
        ttk.Button(buttons, text='Close', command=self.cancel).pack(side='right')
        action = 'Use Selected' if self.lookup else 'Import Selected Beacon'
        self.use_record = ttk.Button(buttons, text=action, command=self.ok)
        self.use_record.pack(side='right', padx=6)
        self.use_all = ttk.Button(buttons, text='Add All', command=self.add_all_shown, state='disabled')
        self.use_all.pack(side='right')
        self.forget = ttk.Button(buttons, text='Forget Saved Login', command=self.clear_saved_login,
                                 state='normal' if self.saved_login else 'disabled')
        self.forget.pack(side='left')
        self.bind('<Escape>', self.cancel)
        self.table.bind('<Return>', self.ok)
        self.password.bind('<Return>', lambda _event: self.start_fetch())
        if self.lookup and self.saved_login:
            self.poll_job = self.after_idle(self.start_fetch)

    def clear_saved_login(self):
        try:
            forget_login()
        except OSError:
            self.note.set('Could not remove the login from Windows Credential Manager.')
            return
        self.saved_login = None
        self.verified_login = None
        self.lock_credentials(False)
        self.username.focus_set()
        self.forget.state(('disabled',))
        self.show_sign_in('out')
        self.note.set('Saved VKSpotter login removed.')

    def lock_credentials(self, locked):
        state = ('disabled',) if locked else ('!disabled',)
        self.username.state(state)
        self.password.state(state)

    def show_sign_in(self, state):
        colour, label = {
            'pending': ('#e7a52b', 'Signing in…'),
            'saved': ('#e7a52b', 'Login saved'),
            'in': ('#a6f326', 'Signed in'),
            'out': ('#e93c46', 'Not signed in'),
        }[state]
        self.sign_in_dot.delete('all')
        self.sign_in_dot.create_oval(2, 2, 12, 12, fill=colour, outline='')
        self.sign_in_text.configure(text=label)

    def add_all_shown(self):
        self.table.selection_set([item for item in self.table.get_children()
                                  if self.records[int(item)]['hz'] is not None])
        self.ok()

    def start_fetch(self):
        if self.fetch.instate(('disabled',)):
            return
        if self.poll_job is not None:
            self.after_cancel(self.poll_job)
            self.poll_job = None
        username, password = self.username.get(), self.password.get()
        known_login = self.verified_login or self.saved_login
        if not password and known_login and username == known_login[0]:
            password = known_login[1]
        if not username.strip() or not password:
            self.note.set('Enter your VKSpotter username and password.')
            return
        if not self.password.instate(('disabled',)):
            self.password.delete(0, 'end')
        self.lock_credentials(True)
        self.fetch.state(('disabled',))
        self.paste.state(('disabled',))
        self.use_record.state(('disabled',))
        self.use_all.state(('disabled',))
        self.forget.state(('disabled',))
        self.show_sign_in('pending')
        self.note.set('Signing in and fetching beacon data…')
        result = Queue(maxsize=1)
        callsign = self.lookup or ''

        def download():
            result.put(fetch_beacons(username, password, callsign=callsign))

        def finish():
            self.poll_job = None
            try:
                fetched = result.get_nowait()
            except Empty:
                self.poll_job = self.after(100, finish)
                return
            self.fetch.state(('!disabled',))
            self.paste.state(('!disabled',))
            self.use_record.state(('!disabled',))
            self.show_sign_in('in' if fetched.signed_in else 'out')
            self.lock_credentials(fetched.signed_in)
            self.verified_login = (username, password) if fetched.signed_in else None
            if fetched.signed_in:
                if self.saved_login != (username, password):
                    try:
                        save_login(username, password)
                    except (OSError, ValueError):
                        messagebox.showwarning('VKSpotter login', 'Signed in, but Windows could not save the login.',
                                               parent=self)
                    else:
                        self.saved_login = (username, password)
            if self.saved_login or self.verified_login:
                self.forget.state(('!disabled',))
            if fetched.error:
                self.note.set(fetched.error)
            else:
                self.load_records(fetched.rows, source='VKSpotter')

        Thread(target=download, daemon=True).start()
        self.poll_job = self.after(100, finish)

    def destroy(self):
        if self.poll_job is not None:
            self.after_cancel(self.poll_job)
        super().destroy()

    def paste_beacons(self):
        try:
            self.load_records(clipboard_beacons(self.clipboard_get()))
        except (tk.TclError, csv.Error):
            self.note.set('Use Copy on the VKSpotter Beacons page, then try Paste Beacon List again.')

    def load_records(self, rows, *, source='Clipboard'):
        records = []
        for row in rows:
            callsign = row['callsign'].upper()
            locator, status = row['grid'].upper(), row['status']
            hz = listed_frequency_hz(row['frequency'])
            if not callsign or (hz is None and not (locator or status)):
                continue
            records.append(dict(callsign=callsign, hz=hz, locator=locator, status=status,
                                location=row.get('location') or row.get('region') or locator or 'Unknown',
                                region=row.get('region', ''), report_date=row.get('report_date', '')))
        if not records:
            self.note.set(f'{source} supplied {len(rows)} rows, but none contained readable beacon details. '
                          'Try copying the VKSpotter table with its column headings included.')
            return
        self.records = records
        self.matches = vkspotter_matches(records, self.lookup) if self.lookup else records
        self.refresh_rows()
        if self.lookup and len(self.matches) == 1:
            self.ok()

    def refresh_rows(self, _event=None):
        self.table.delete(*self.table.get_children())
        query = self.search.get().strip().casefold()
        for index, record in enumerate(self.records):
            if self.lookup and record not in self.matches:
                continue
            frequency = f"{record['hz'] / 1e6:.6f}" if record['hz'] is not None else '—'
            search_text = ' '.join((record['callsign'], record['location'], record['locator'],
                                    record['region'], record['status'], frequency)).casefold()
            if query in search_text:
                values = (record['callsign'], record['locator'], record['location'], frequency, record['status'],
                          format_beacon_path(self.coordinates, record['locator']))
                band = beacon_band(record['hz'])
                self.table.insert('', 'end', iid=str(index), values=values, tags=(band,) if band else ())
        shown = self.table.get_children()
        usable = sum(self.records[int(item)]['hz'] is not None for item in shown)
        self.use_all.state(('!disabled',) if usable > 1 and self.poll_job is None else ('disabled',))
        if self.records:
            count = len(shown)
            detail = ' Ctrl+click or Shift+click to select multiple frequencies.' if usable > 1 else ''
            if count > usable and usable > 1:
                detail += ' Add All uses displayed rows with a readable frequency.'
            self.note.set(f'{count} of {len(self.records)} beacon records shown.{detail}' if count
                          else 'No matches in this list. Check the callsign or try a fresh VKSpotter list.')

    def validate(self):
        if self.poll_job is not None:
            return False
        if self.lookup and len(self.matches) == 1:
            self.result = self.matches[0]
            return True
        selected = self.table.selection()
        if selected:
            records = [self.records[int(item)] for item in selected]
            if (len(records) > 1 or not self.lookup) and any(record['hz'] is None for record in records):
                self.note.set('Select rows with a readable frequency, or select one row to enter its frequency manually.')
                return False
            self.result = records[0] if len(records) == 1 else records
            return True
        self.note.set('Select a beacon to import.')
        return False


class BeaconEditor(simpledialog.Dialog):
    def __init__(self, parent, plotter, beacon=None):
        self.plotter = plotter
        self.beacon = beacon
        self.lookup_record = beacon
        self.lookup_identity = beacon_identity(beacon) if beacon else None
        self.batch_records = None
        self.callsign_text = tk.StringVar(parent)
        super().__init__(parent, 'Edit Beacon' if beacon else ADD_BEACON)

    def body(self, frame):
        background = DISPLAY_THEMES[self.plotter.active_theme]['background']
        self.configure(background=background)
        frame.configure(background=background)
        frame.columnconfigure(1, weight=1)
        defaults = ('', '', '', '')
        if self.beacon:
            defaults = (self.beacon['callsign'], self.beacon['location'], f"{self.beacon['hz'] / 1e6:.6f}",
                        self.beacon.get('locator') or '')
        self.fields = []
        for row, (label, default) in enumerate(zip(
            ('Beacon Callsign', 'Beacon Location', 'Frequency in MHz', 'Maidenhead locator (optional)'), defaults,
        )):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=6, pady=4)
            entry = ttk.Entry(frame, width=34, textvariable=self.callsign_text if row == 0 else None)
            entry.grid(row=row, column=1, sticky='ew', padx=6, pady=4)
            entry.insert(0, default)
            self.fields.append(entry)
        self.lookup_button = ttk.Button(frame, text='Look Up on VKSpotter…', command=self.lookup)
        self.lookup_button.grid(row=4, column=1, sticky='w', padx=6, pady=4)
        ttk.Label(frame, wraplength=370, justify='left',
                  text='With an active VKSpotter login, Look Up on VKSpotter can fill in the '
                       'location and frequency for a matching callsign.').grid(
            row=5, column=0, columnspan=2, sticky='w', padx=6, pady=(8, 4))
        self.lookup_summary = tk.StringVar(self)
        self.lookup_details = ttk.Label(frame, textvariable=self.lookup_summary, justify='left', wraplength=370)
        self.lookup_details.grid(
            row=6, column=0, columnspan=2, sticky='w', padx=6, pady=4)
        self.show_lookup()
        return self.fields[0]

    def buttonbox(self):
        buttons = ttk.Frame(self, padding=(0, 0, 0, 8))
        buttons.pack()
        self.save_button = ttk.Button(buttons, text='OK', command=self.ok, default='active')
        self.save_button.pack(side='left', padx=5)
        ttk.Button(buttons, text='Cancel', command=self.cancel).pack(side='left', padx=5)
        self.bind('<Return>', self.submit)
        self.bind('<Escape>', self.cancel)
        self.callsign_trace = self.callsign_text.trace_add('write', self.update_field_state)
        self.update_field_state()

    def update_field_state(self, *_):
        state = ('!disabled',) if beacon_callsign_key(self.callsign_text.get()) else ('disabled',)
        for widget in (*self.fields[1:], self.lookup_button, self.save_button):
            widget.state(state)

    def submit(self, _event):
        if not self.save_button.instate(('disabled',)):
            self.ok()

    def destroy(self):
        self.callsign_text.trace_remove('write', self.callsign_trace)
        super().destroy()

    def lookup(self):
        callsign = beacon_callsign_key(self.fields[0].get())
        if not callsign:
            messagebox.showinfo('VKSpotter lookup', 'Enter the beacon callsign first.', parent=self)
            self.fields[0].focus_set()
            return
        frequency = self.fields[2].get().strip()
        try:
            hz = beacon_frequency_hz(frequency) if frequency else None
        except ValueError as exc:
            messagebox.showwarning('Beacon frequency', str(exc), parent=self)
            self.fields[2].focus_set()
            return
        try:
            dialog = VKSpotterDialog(self, self.plotter.station_coordinates, self.plotter.active_theme,
                                     lookup=callsign)
        finally:
            self.grab_set()
        record = dialog.result
        if isinstance(record, list):
            self.batch_records = record
            self.ok()
            return
        if record:
            if len(dialog.matches) > 1 and record['hz'] is not None:
                hz = record['hz']
                self.fields[2].delete(0, 'end')
                self.fields[2].insert(0, f'{hz / 1e6:.6f}')
            self.lookup_record = record
            self.lookup_identity = callsign, record['hz'] if hz is None else hz
            if not self.fields[1].get().strip():
                self.fields[1].insert(0, record['location'])
            if hz is None and record['hz'] is not None:
                self.fields[2].insert(0, f"{record['hz'] / 1e6:.6f}")
            if record.get('locator'):
                self.fields[3].delete(0, 'end')
                self.fields[3].insert(0, record['locator'])
            self.show_lookup()

    def show_lookup(self):
        record = self.lookup_record or {}
        locator, status = record.get('locator'), record.get('status')
        if not (locator or status):
            self.lookup_summary.set('')
            self.lookup_details.grid_remove()
            return
        details = f"Grid {locator or '—'}  ·  VKSpotter status {status or '—'}"
        if record.get('report_date'):
            details += f"\nReported {record['report_date']}"
        if record.get('hz') is None and not self.fields[2].get().strip():
            details += '\nEnter the frequency in MHz before saving.'
        self.lookup_summary.set(details)
        self.lookup_details.grid()

    def validate(self):
        if self.batch_records:
            self.result = self.batch_records
            return True
        try:
            self.result = beacon_record(*(field.get() for field in self.fields[:3]))
            if self.lookup_identity:
                callsign, hz = self.lookup_identity
                new_callsign, new_hz = beacon_identity(self.result)
                if callsign == new_callsign and (hz is None or hz == new_hz):
                    self.result = {**self.lookup_record, **self.result}
            locator = self.fields[3].get().strip().upper()
            if locator:
                previous = (self.lookup_record or {}).get('locator')
                if locator != previous:
                    try:
                        maidenhead_coordinates(locator, allow_four=True)
                    except ValueError:
                        raise ValueError('Enter a valid 4-, 6- or 8-character Maidenhead locator.') from None
                self.result['locator'] = locator
            else:
                self.result.pop('locator', None)
        except ValueError as exc:
            messagebox.showwarning('Beacon details', str(exc), parent=self)
            return False
        return True


def query(port, address, pending, payload, timeout, reply=None):
    command = bytes((0xFE, 0xFE, address, 0xE0)) + payload + b'\xfd'
    port.write(command)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending.extend(port.read(port.in_waiting or 1))
        while 0xFD in pending:
            end = pending.index(0xFD) + 1
            frame = bytes(pending[:end])
            del pending[:end]
            start = frame.rfind(b'\xfe\xfe')
            if start < 0:
                continue
            frame = frame[start:]
            if frame == bytes((0xFE, 0xFE, 0xE0, address, 0xFA, 0xFD)):
                raise ValueError('The radio rejected the CI-V command.')
            prefix = bytes((0xFE, 0xFE, 0xE0, address)) + (payload if reply is None else reply)
            if frame.startswith(prefix):
                return frame[len(prefix):-1]
        if len(pending) > 4096:
            pending.clear()
    return None


def read_meter(port, address, pending):
    data = query(port, address, pending, b'\x15\x02', 0.8)
    if data is None:
        return None
    digits = data.hex()
    if len(data) != 2 or not digits.isdecimal() or int(digits) > 255:
        raise ValueError('Invalid S-meter reply from the radio.')
    return int(digits)


def read_frequency(port, address, pending):
    data = query(port, address, pending, b'\x03', 0.25)
    if data is None or len(data) not in (5, 6):
        return None
    digits = data[::-1].hex()
    return int(digits) if digits.isdecimal() else None


def tune_beacon(port, address, pending, hz):
    width = 12 if hz >= 10_000_000_000 else 10
    frequency = bytes.fromhex(f'{hz:0{width}d}')[::-1]
    commands = (
        (b'\x05' + frequency, 'frequency'),
        (b'\x06\x03\x01', 'CW / Filter 1'),
    )
    for payload, name in commands:
        try:
            acknowledgement = query(port, address, pending, payload, 0.8, reply=b'\xfb')
        except ValueError as exc:
            raise ValueError(f'The radio rejected the {name} setting.') from exc
        if acknowledgement is None:
            raise ValueError(f'No acknowledgement for the {name} setting.')
        if name == 'frequency' and read_frequency(port, address, pending) != hz:
            raise ValueError('Frequency readback did not match the beacon.')
    mode = query(port, address, pending, b'\x04', 0.5)
    if mode is None or mode[:2] != b'\x03\x01':
        raise ValueError('The radio did not confirm CW / Filter 1.')


def set_radio_mute(port, address, pending, previous_volume):
    """Mute the selected receiver, or restore its saved AF level."""
    command = b'\x14\x01'
    current = query(port, address, pending, command, 0.5)
    if current is None or len(current) != 2 or not current.hex().isdecimal() or int(current.hex()) > 255:
        raise ValueError('Could not read the radio volume.')
    zero = b'\x00\x00'
    target = zero if previous_volume is None else previous_volume
    if current != target and (previous_volume is None or current == zero):
        query(port, address, pending, command + target, 0.5, reply=b'\xfb')
        if query(port, address, pending, command, 0.5) != target:
            raise ValueError('The radio did not confirm the requested volume.')
    return current if previous_volume is None and current != zero else None


def sample_radio(port_name, baud, address, stop, pause, events, radio_requests, sample_interval):
    import serial

    try:
        with serial.Serial(port_name, baud, timeout=0.1, write_timeout=0.5) as port:
            pending = bytearray()
            next_frequency = 0.0
            last_sample = float("-inf")
            meter_available = False
            was_paused = False
            while not stop.is_set():
                started = time.monotonic()
                try:
                    kind, argument = radio_requests.get_nowait()
                except Empty:
                    kind, argument = None, None
                if kind == 'audio':
                    try:
                        restore_level = set_radio_mute(port, address, pending, argument)
                    except ValueError as exc:
                        events.put(('audio_failed', str(exc)))
                    else:
                        events.put(('audio_set', restore_level))
                if kind == 'beacon':
                    beacon = argument
                    events.put(('plot_gap', time.monotonic()))
                    try:
                        tune_beacon(port, address, pending, beacon['hz'])
                    except ValueError as exc:
                        events.put(('tune_failed', str(exc)))
                    else:
                        events.put(('tuned', beacon))
                    next_frequency = 0.0
                    last_sample = float('-inf')
                    continue
                if pause.is_set():
                    was_paused = True
                    stop.wait(0.1)
                    continue
                if was_paused:
                    events.put(('plot_gap', started))
                    next_frequency = 0.0
                    last_sample = float('-inf')
                    was_paused = False
                if started - last_sample >= sample_interval():
                    value = read_meter(port, address, pending)
                    if pause.is_set():
                        was_paused = True
                        continue
                    last_sample = started
                    meter_available = value is not None
                    events.put(('sample', (time.monotonic(), value)))
                if started >= next_frequency and not stop.is_set() and not pause.is_set():
                    frequency = None
                    if meter_available:
                        try:
                            frequency = read_frequency(port, address, pending)
                        except ValueError:
                            pass
                    events.put(('frequency', frequency))
                    next_frequency = started + 1.0
                stop.wait(min(0.1, max(0, last_sample + sample_interval() - time.monotonic())))
    except (OSError, ValueError, OverflowError, serial.SerialException) as exc:
        events.put(('error', str(exc)))
    finally:
        events.put(('closed', None))


def copy_window_image(widget_id):
    """Capture only this window and give its bitmap to the Windows clipboard."""
    if os.name != 'nt':
        raise OSError('Screenshot requires Windows.')
    from PIL import ImageGrab

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = (
        (user32.GetAncestor, (wintypes.HWND, wintypes.UINT), wintypes.HWND),
        (user32.OpenClipboard, (wintypes.HWND,), wintypes.BOOL),
        (user32.EmptyClipboard, (), wintypes.BOOL),
        (user32.SetClipboardData, (wintypes.UINT, wintypes.HANDLE), wintypes.HANDLE),
        (user32.CloseClipboard, (), wintypes.BOOL),
        (kernel32.GlobalAlloc, (wintypes.UINT, ctypes.c_size_t), wintypes.HANDLE),
        (kernel32.GlobalLock, (wintypes.HANDLE,), ctypes.c_void_p),
        (kernel32.GlobalUnlock, (wintypes.HANDLE,), wintypes.BOOL),
        (kernel32.GlobalFree, (wintypes.HANDLE,), wintypes.HANDLE),
    )
    for function, argtypes, restype in signatures:
        function.argtypes = argtypes
        function.restype = restype

    window = user32.GetAncestor(widget_id, 2)  # GA_ROOT includes the title bar.
    if not window:
        raise OSError('Could not locate the plotter window.')
    with ImageGrab.grab(window=window) as image, BytesIO() as bitmap:
        image.convert('RGB').save(bitmap, 'BMP')
        dib = bitmap.getvalue()[14:]  # CF_DIB omits the BMP file header.

    handle = kernel32.GlobalAlloc(0x0002, len(dib))  # GMEM_MOVEABLE
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        memory = kernel32.GlobalLock(handle)
        if not memory:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(memory, dib, len(dib))
        finally:
            kernel32.GlobalUnlock(handle)

        for attempt in range(5):
            if user32.OpenClipboard(window):
                break
            if attempt < 4:
                time.sleep(0.025)
        else:
            raise OSError('The clipboard is busy. Try copying the screenshot again.')
        try:
            if not user32.EmptyClipboard() or not user32.SetClipboardData(8, handle):  # CF_DIB
                raise ctypes.WinError(ctypes.get_last_error())
            handle = None  # Windows owns the memory after SetClipboardData succeeds.
        finally:
            user32.CloseClipboard()
    finally:
        if handle:
            kernel32.GlobalFree(handle)


class Plotter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'VK3FS Beacon Strength Plotter v{APP_VERSION}')
        if os.name == 'nt':
            icon_path = Path(__file__).resolve().with_name('radio_tower.ico')
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass  # The window can still open if Windows cannot load the icon.
        self.geometry('1024x780')
        self.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.events = Queue()
        self.history = deque()
        self.plot_cleared_at = float('-inf')
        self.time_offset = time.time() - time.monotonic()
        self.loaded_plot = False
        self.last_draw = None
        self.view_end = None
        self.stop = None
        self.radio_responding = False
        self.pause = Event()
        self.pause_time = None
        self.settings_path = settings_file()
        first_run = not self.settings_path.exists()
        settings = load_settings(self.settings_path)
        smoothing = settings.get('smoothing')
        self.smoothing = tk.StringVar(value=smoothing if smoothing in tuple(SMOOTHING_WINDOWS) else 'Off')
        saved_theme = settings.get('display_theme')
        self.display_theme = tk.StringVar(value=saved_theme if saved_theme in DISPLAY_MODES else 'Day')
        self.active_theme = None
        self.display_mode_preview = None
        self.setup_dialog = None
        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.beacons = list(settings.get('beacons', DEFAULT_BEACONS))
        self.tuning = False
        self.restore_volume = None
        self.audio_pending = False
        self.auto_connecting = False
        self.current_hz = None
        self.baud = tk.StringVar(value=settings.get('baud', '115200'))
        model, address = radio_setup(settings)
        saved_ports = settings.get('radio_ports', {})
        self.radio_ports = saved_ports if isinstance(saved_ports, dict) else {}
        self.last_connected_radio = startup_radio(settings, model, self.radio_ports)
        # Use the old global port as a starting suggestion until verified.
        legacy_port = settings.get('port', '') if 'radio_ports' not in settings else ''
        self.port = tk.StringVar(value=self.radio_ports.get(model, legacy_port))
        self.model = tk.StringVar(value=model)
        self.plot_model = model
        self.address = tk.StringVar(value=address)
        units = settings.get('axis_units')
        self.axis_units = tk.StringVar(value=units if units in AXIS_UNITS else AXIS_UNITS[0])
        reference = settings.get('dbm_reference')
        self.dbm_reference = tk.StringVar(value=reference if reference in tuple(DBM_REFERENCES) else DEFAULT_DBM_REFERENCE)
        transparency = settings.get('plot_transparency')
        self.plot_transparency = transparency if type(transparency) is int and 0 <= transparency <= 100 else 35
        self.plot_transparency_preview = None
        self.plot_wallpaper_layers = None
        self.warn_on_close = settings.get('warn_on_close') is not False
        self.auto_connect = settings.get('auto_connect') is not False
        callsign = settings.get('station_callsign')
        locator = settings.get('station_locator')
        self.station_callsign = callsign.strip().upper() if isinstance(callsign, str) else ''
        self.station_locator = locator.strip().upper() if isinstance(locator, str) else ''
        self.station_coordinates = None
        key = settings.get('google_maps_key')
        self.google_maps_key = key if isinstance(key, str) else ''
        self.map_server = None
        self.synoptic_window = None
        source = settings.get('synoptic_source', DEFAULT_SYNOPTIC_CHART)
        self.synoptic_source = source.strip() if isinstance(source, str) else DEFAULT_SYNOPTIC_CHART
        if self.synoptic_source:
            try:
                validate_chart_url(self.synoptic_source)
            except ValueError:
                self.synoptic_source = DEFAULT_SYNOPTIC_CHART
        source = settings.get('hepburn_source', '')
        self.hepburn_source = source.strip() if isinstance(source, str) else ''
        if self.hepburn_source:
            try:
                validate_chart_url(self.hepburn_source)
            except ValueError:
                self.hepburn_source = ''
        if self.station_locator:
            try:
                self.station_coordinates = maidenhead_coordinates(self.station_locator)
            except ValueError:
                self.station_locator = ''
        self.status = tk.StringVar(value='Select the radio and its CI-V COM port.')
        self.readout = tk.StringVar(value='Raw CI-V  —')
        saved_interval = settings.get('sample_interval_seconds')
        self.sample_interval = float(saved_interval) if saved_interval in SAMPLE_INTERVALS else 0.5
        self.frequency = tk.StringVar(value=format_radio_frequency(None))
        self.window_index = tk.IntVar(value=0)
        self.window_label = tk.StringVar(value=TIME_WINDOWS[0][1])
        self.backdrop_key = None
        self.backdrop_photo = None
        self.backdrop_resize_job = None
        self.backdrop = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self.backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, minsize=92)
        self.rowconfigure(4, weight=1)

        # Let the frames size the controls independently of the canvases that paint their backgrounds.
        radio_panel = tk.Frame(self)
        self.radio_backdrop = tk.Canvas(radio_panel, borderwidth=0, highlightthickness=0)
        self.radio_backdrop.place(relwidth=1, relheight=1)
        radio_panel.rowconfigure(0, minsize=40)
        radio_panel.grid(row=1, column=0, sticky='nsew', padx=(22, 4), pady=(0, 6))
        self.radio_picker = ttk.Combobox(radio_panel, textvariable=self.model,
                                        values=tuple(RADIO_ADDRESSES), width=16, state='readonly')
        self.radio_picker.grid(row=1, column=0, padx=(10, 12), sticky='ew')
        radio_labels = [('Radio model', self.radio_picker)]
        self.radio_picker.bind('<<ComboboxSelected>>', self.change_radio)
        self.inputs = []
        for column, (label, variable, values, width) in enumerate((
            ('COM port', self.port, (), 9),
            ('Baud rate', self.baud, (4800, 9600, 19200, 38400, 57600, 115200), 10),
            ('CI-V address', self.address, None, 9),
        ), start=1):
            field = (ttk.Entry(radio_panel, textvariable=variable, width=width) if values is None else
                     ttk.Combobox(radio_panel, textvariable=variable, values=values, width=width))
            field.grid(row=1, column=column, padx=(0, 12), sticky='ew')
            self.inputs.append(field)
            radio_labels.append((label, field))
        self.refresh = ttk.Button(radio_panel, text='Refresh ports', command=self.refresh_ports)
        self.refresh.grid(row=2, column=0, sticky='w', padx=(10, 0), pady=(5, 8))
        self.connect = ttk.Button(radio_panel, text='Connect', command=self.toggle_connection)
        self.connect.grid(row=2, column=2, columnspan=2, sticky='e', padx=(0, 12), pady=(5, 8))
        beacon_panel = tk.Frame(self)
        beacon_backdrop = tk.Canvas(beacon_panel, borderwidth=0, highlightthickness=0)
        beacon_backdrop.place(relwidth=1, relheight=1)
        beacon_panel.columnconfigure(0, weight=1)
        beacon_panel.rowconfigure(0, minsize=40)
        beacon_panel.grid(row=1, column=1, sticky='nsew', padx=(4, 22), pady=(0, 6))
        self.beacon_picker = ttk.Combobox(beacon_panel, width=25, state='readonly',
                                         postcommand=self.prepare_beacon_dropdown)
        self.beacon_picker.grid(row=1, column=0, columnspan=2, sticky='ew', padx=10)
        self.beacon_picker.bind('<<ComboboxSelected>>', self.select_beacon)
        self.beacon_path_label = tk.StringVar(self)
        ttk.Label(beacon_panel, textvariable=self.beacon_path_label).grid(
            row=2, column=0, sticky='w', padx=10, pady=(5, 8),
        )
        self.refresh_beacons()
        ttk.Button(beacon_panel, text='Manage Beacons', command=self.manage_beacons).grid(
            row=2, column=1, sticky='e', padx=10, pady=(5, 8),
        )
        self.setup_panels = (
            (radio_panel, self.radio_backdrop, 'Radio connection', radio_labels),
            (beacon_panel, beacon_backdrop, 'Beacon', ()),
        )
        readouts = ttk.Frame(self, padding=(14, 4))
        readouts.grid(row=2, column=0, columnspan=2, sticky='ew', padx=22)
        ttk.Button(readouts, text='Setup', command=self.open_setup).pack(side='left')
        files = ttk.Menubutton(readouts, text='Plot files')
        files.pack(side='left', padx=(8, 0))
        menu = tk.Menu(files, tearoff=False)
        menu.add_command(label='Save plot…', command=self.save_plot)
        menu.add_command(label='Open saved plot…', command=self.open_plot)
        menu.add_separator()
        menu.add_command(label='Export CSV…', command=self.export_csv)
        files.configure(menu=menu)
        ttk.Button(readouts, text='Map', command=self.open_beacon_map).pack(side='left', padx=(8, 0))
        ttk.Button(readouts, text='Synoptic', command=self.open_synoptic_chart).pack(side='left', padx=(8, 0))
        ttk.Button(readouts, text='Hepburn Chart', command=self.open_hepburn_chart).pack(
            side='left', padx=(8, 0))
        self.mute_button = ttk.Button(readouts, text='Mute radio', state='disabled',
                                      command=self.toggle_radio_mute)
        self.mute_button.pack(side='right', padx=(8, 0))
        ttk.Label(readouts, textvariable=self.frequency, font=('Segoe UI', 15)).pack(side='right')
        self.after_idle(self.update_toolbar_minimum_width, readouts)
        range_bar = ttk.Frame(self, padding=(14, 0, 14, 6))
        range_bar.grid(row=3, column=0, columnspan=2, sticky='ew', padx=22)
        ttk.Label(range_bar, text='Zoom').pack(side='left')
        ttk.Button(range_bar, text='−', width=3, command=lambda: self.zoom(1)).pack(side='left', padx=(6, 0))
        self.zoom_slider = tk.Scale(range_bar, from_=0, to=len(TIME_WINDOWS) - 1, orient='horizontal',
                                    showvalue=False, resolution=1, variable=self.window_index,
                                    command=self.change_window, highlightthickness=0)
        self.zoom_slider.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(range_bar, text='+', width=3, command=lambda: self.zoom(-1)).pack(side='left')
        ttk.Label(range_bar, textvariable=self.window_label, width=7).pack(side='left', padx=(8, 0))
        self.live_button = ttk.Button(range_bar, text='Live', command=self.follow_live, state='disabled')
        self.live_button.pack(side='right')
        self.pause_button = ttk.Button(range_bar, text='Pause', state='disabled', command=self.toggle_pause)
        self.pause_button.pack(side='right', padx=(0, 6))
        self.clear_plot_button = ttk.Button(range_bar, text='Clear Plot', state='disabled', command=self.clear_plot)
        self.clear_plot_button.pack(side='right', padx=(0, 6))
        self.screenshot_button = ttk.Button(range_bar, text='Screenshot', command=self.copy_screenshot)
        self.screenshot_button.pack(side='right', padx=(0, 6))
        self.chart = tk.Canvas(self, highlightthickness=0)
        self.chart.wallpaper_photo = None
        self.chart.grid(row=4, column=0, columnspan=2, sticky='nsew', padx=22)
        self.y_zoom = tk.IntVar(self, value=1)
        self.y_zoom_controls = ttk.Frame(self.chart, padding=(3, 3))
        self.y_zoom_controls.place(relx=1.0, x=-4, y=Y_ZOOM_TOP, anchor='ne', relheight=1.0,
                                   height=-(Y_ZOOM_TOP + PLOT_BOTTOM_MARGIN))
        ttk.Label(self.y_zoom_controls, text='Y zoom').pack()
        self.y_zoom_slider = tk.Scale(self.y_zoom_controls, from_=16, to=1, orient='vertical',
                                      variable=self.y_zoom, highlightthickness=0,
                                      command=self.change_y_zoom)
        self.y_zoom_slider.pack(fill='y', expand=True)
        self.chart.bind('<MouseWheel>', self.wheel_zoom)
        self.history_scroll = ttk.Scrollbar(self, orient='horizontal', command=self.scroll_history)
        self.history_scroll.grid(row=5, column=0, columnspan=2, sticky='ew', padx=22)
        self.chart.bind('<Configure>', lambda event: self.draw())
        self.footer = tk.Canvas(self, width=1, height=34, highlightthickness=0)
        self.footer.grid(row=6, column=0, columnspan=2, sticky='ew', padx=22, pady=(6, 18))
        self.setup_panels += ((self.footer, self.footer, None, ()),)
        self.status.trace_add('write', self.update_footer)
        self.readout.trace_add('write', self.update_footer)
        self.bind('<Configure>', self.schedule_backdrop_resize)
        for container, _, _, _ in self.setup_panels:
            container.bind('<Configure>', self.schedule_backdrop_resize)
        self.chart.bind('<Configure>', self.schedule_backdrop_resize, add='+')
        self.apply_display_theme()
        self.refresh_ports()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(100, self.poll)
        self.after(1000, self.update_display_clock)
        if first_run:
            self.after_idle(self.prompt_first_run_station)
        elif self.auto_connect and self.last_connected_radio:
            self.after_idle(self.reconnect_last_radio)

    def prompt_first_run_station(self):
        result = FirstRunStationDialog(self, 'Your station').result
        if result:
            self.station_callsign, self.station_locator, self.station_coordinates = result
            self.sync_beacon()
        self.persist_settings()

    def open_hepburn_chart(self):
        if self.hepburn_source:
            webbrowser.open(self.hepburn_source)
            return
        if messagebox.askyesno(
            'Hepburn Chart',
            'Choose a forecast at the Hepburn chart index, then copy its URL and paste it into '
            'Setup → Hepburn Chart.\n\nOpen the chart index now?',
            parent=self,
        ):
            webbrowser.open(HEPBURN_CHART_INDEX)

    def update_toolbar_minimum_width(self, toolbar):
        padding = int(toolbar.grid_info()['padx'])
        width = max(MIN_WINDOW_WIDTH, toolbar.winfo_reqwidth() + 2 * padding)
        self.minsize(width, MIN_WINDOW_HEIGHT)

    def apply_display_theme(self):
        theme = effective_theme(self.display_mode_preview or self.display_theme.get(), datetime.now().hour)
        if theme == self.active_theme:
            return
        self.active_theme = theme
        palette = DISPLAY_THEMES[theme]
        background, text = palette['background'], palette['text']
        self.configure(background=background)
        self.style.configure('.', background=background, foreground=text, bordercolor=palette['border'],
                             lightcolor=palette['border'], darkcolor=palette['border'],
                             selectbackground=palette['selection'], selectforeground='#ffffff')
        for widget in ('TButton', 'Horizontal.TScrollbar', 'Vertical.TScrollbar'):
            self.style.configure(widget, background=palette['control'], arrowcolor=text,
                                 troughcolor=background)
            self.style.map(widget, background=[('active', palette['hover'])],
                           foreground=[('disabled', palette['muted'])])
        for widget in ('TRadiobutton', 'TCheckbutton'):
            self.style.configure(widget, background=background, foreground=text,
                                 indicatorbackground=palette['panel'])
            self.style.map(widget, background=[('active', palette['hover'])],
                           indicatorbackground=[('selected', palette['selection']), ('!selected', palette['panel'])])
        for widget in ('TCombobox', 'TEntry'):
            self.style.configure(widget, fieldbackground=palette['panel'], insertcolor=text, arrowcolor=text)
            self.style.map(widget, fieldbackground=[('disabled', background), ('readonly', palette['panel'])],
                           foreground=[('disabled', palette['muted']), ('readonly', text)])
        self.option_add('*Toplevel.background', background)
        self.option_add('*Listbox.background', palette['panel'])
        self.option_add('*Listbox.foreground', text)
        self.option_add('*Listbox.selectBackground', palette['selection'])
        self.option_add('*Listbox.selectForeground', '#ffffff')
        for slider in (self.zoom_slider, self.y_zoom_slider):
            slider.configure(background=palette['control'], troughcolor=palette['panel'],
                             activebackground=palette['hover'], foreground=text)
        self.chart.configure(background=palette['panel'])
        if self.setup_dialog is not None:
            self.setup_dialog.apply_palette()
        self.update_backdrop()

    def update_display_clock(self):
        if (self.display_mode_preview or self.display_theme.get()) == 'Auto':
            self.apply_display_theme()
        self.draw_clocks()
        self.after(1000, self.update_display_clock)

    def refresh_beacons(self):
        self.beacons.sort(key=lambda beacon: (
            beacon['callsign'].casefold(), beacon['location'].casefold(), beacon['hz'],
        ))
        self.beacon_picker.configure(values=(*map(beacon_label, self.beacons), ADD_BEACON))
        self.sync_beacon()

    def prepare_beacon_dropdown(self):
        # Tk fills the combobox's listbox after postcommand and before idle handlers.
        self.after_idle(self.colour_beacon_dropdown)

    def colour_beacon_dropdown(self):
        widget = self.beacon_picker
        call = widget.tk.call
        palette = DISPLAY_THEMES[self.active_theme]
        try:
            popdown = call('ttk::combobox::PopdownWindow', widget._w)
            rows = f'{popdown}.f.l'
            call(rows, 'configure', '-background', palette['panel'], '-foreground', palette['text'],
                 '-selectbackground', palette['selection'], '-selectforeground', '#ffffff')
            for index, beacon in enumerate(self.beacons):
                call(rows, 'itemconfigure', index, '-background',
                     beacon_row_background(beacon['hz'], self.active_theme))
            call(rows, 'itemconfigure', len(self.beacons), '-background', palette['panel'])
        except tk.TclError:
            # Some Tk implementations provide a native menu without a listbox.
            pass

    def sync_beacon(self):
        if self.tuning:
            return
        selected = self.beacon_picker.current()
        if not (0 <= selected < len(self.beacons) and
                (self.current_hz is None or self.beacons[selected]['hz'] == self.current_hz)):
            selected = next((index for index, beacon in enumerate(self.beacons)
                             if beacon['hz'] == self.current_hz), -1)
            if selected < 0:
                self.beacon_picker.set('')
            else:
                self.beacon_picker.current(selected)
        locator = self.beacons[selected].get('locator') if selected >= 0 else None
        self.beacon_path_label.set(format_beacon_path(self.station_coordinates, locator, labelled=True))

    def import_vkspotter_beacons(self, records):
        indices = {beacon_identity(beacon): index for index, beacon in reversed(list(enumerate(self.beacons)))}
        for record in records:
            key = beacon_identity(record)
            if key in indices:
                index = indices[key]
                existing = self.beacons[index]
                self.beacons[index] = {**record, 'location': existing['location'], 'callsign': existing['callsign']}
            else:
                indices[key] = len(self.beacons)
                self.beacons.append(record)
        self.refresh_beacons()
        self.persist_settings()
        count = len({beacon_identity(record) for record in records})
        self.status.set(f'Imported VKSpotter data for {count} beacon {"frequency" if count == 1 else "frequencies"}.')

    def show_frequency(self, hz):
        self.current_hz = hz
        self.frequency.set(format_radio_frequency(hz))
        self.sync_beacon()

    def open_beacon_map(self):
        index = self.beacon_picker.current()
        if not 0 <= index < len(self.beacons):
            messagebox.showinfo('Beacon map', 'Select a beacon to map.', parent=self)
            return
        if self.station_coordinates is None:
            messagebox.showinfo('Beacon map', 'Enter your station locator in Setup.', parent=self)
            return
        beacon = self.beacons[index]
        locator = beacon.get('locator')
        try:
            destination = maidenhead_coordinates(locator or '', allow_four=True)
        except (AttributeError, ValueError):
            messagebox.showinfo('Beacon map', 'Enter a valid Maidenhead locator for this beacon.', parent=self)
            return
        station_name = self.station_callsign or 'Station'
        details = (station_name, beacon['callsign'], self.station_coordinates, destination,
                   format_beacon_path(self.station_coordinates, locator))
        if not self.google_maps_key:
            result = MapKeyDialog(self, '', opening_map=True).result
            if result is None:
                return
            if result[0] == 'export':
                self.export_beacon_map(details)
                return
            self.google_maps_key = result[1]
            self.persist_settings()
        try:
            if self.map_server is None:
                self.map_server = BeaconMapServer()
            url = self.map_server.publish(map_page(self.google_maps_key, *details, elevation=True))
            if not webbrowser.open(url):
                raise OSError('The default browser could not be opened.')
        except OSError as exc:
            messagebox.showwarning('Beacon map', str(exc), parent=self)

    def open_synoptic_chart(self):
        from PIL import Image, ImageTk

        source = self.synoptic_source
        if not source:
            messagebox.showinfo('Synoptic', 'Set an image URL in Setup → Synoptic chart.', parent=self)
            return
        if self.synoptic_window is not None and self.synoptic_window.winfo_exists():
            self.synoptic_window.lift()
            return
        window = self.synoptic_window = tk.Toplevel(self)
        window.title('Synoptic')
        window.transient(self)
        window.bind('<Escape>', lambda _event: window.destroy())
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill='both', expand=True)
        chart = ttk.Label(frame, text='Loading synoptic chart…')
        chart.pack(fill='both', expand=True)
        ttk.Button(frame, text='Close', command=window.destroy).pack(pady=(8, 0))
        screen_size = (int(window.winfo_screenwidth() * 0.85), int(window.winfo_screenheight() * 0.80))
        result = Queue(maxsize=1)

        def fetch():
            try:
                result.put(read_synoptic_chart(source, screen_size))
            except (OSError, ValueError, Image.DecompressionBombError):
                result.put(None)

        def show():
            if not window.winfo_exists():
                return
            try:
                image = result.get_nowait()
            except Empty:
                self.after(100, show)
                return
            if image is None:
                chart.configure(text='Could not load the chart. Check the image URL in Setup.')
            else:
                chart.image = ImageTk.PhotoImage(image, master=window)
                chart.configure(image=chart.image, text='')

        Thread(target=fetch, daemon=True).start()
        self.after(100, show)

    def export_beacon_map(self, details):
        path = filedialog.asksaveasfilename(parent=self, title='Export beacon path', defaultextension='.kml',
                                            initialfile='Beacon path.kml', filetypes=[('KML map', '*.kml')])
        if not path:
            return
        try:
            write_path_kml(path, *details)
        except OSError as exc:
            messagebox.showwarning('Beacon map', str(exc), parent=self)
            return
        if messagebox.askyesno('Beacon map', 'KML saved. Open Google My Maps to import it?', parent=self):
            webbrowser.open(MY_MAPS_URL)

    def select_beacon(self, _event):
        index = self.beacon_picker.current()
        if index == len(self.beacons):
            self.beacon_picker.set('')
            entry = BeaconEditor(self, self).result
            if isinstance(entry, list):
                self.import_vkspotter_beacons(entry)
            elif entry:
                self.beacons.append(entry)
                self.refresh_beacons()
                self.persist_settings()
            self.sync_beacon()
            return
        if index < 0:
            return
        beacon = self.beacons[index]
        self.beacon_path_label.set(format_beacon_path(self.station_coordinates, beacon.get('locator'), labelled=True))
        if self.stop is None or self.stop.is_set() or self.auto_connecting:
            return
        self.tuning = True
        self.beacon_picker.configure(state='disabled')
        self.status.set(f"Tuning to {beacon['callsign']}…")
        self.radio_requests.put_nowait(('beacon', beacon))

    def manage_beacons(self):
        window = tk.Toplevel(self)
        window.withdraw()
        window.title('Manage Beacons')
        window.transient(self)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill='both', expand=True)
        rows = ttk.Frame(frame)
        rows.pack(fill='both', expand=True)
        rows.columnconfigure(0, weight=1)
        rows.rowconfigure(0, weight=1)
        entries = tk.Listbox(rows, width=1, height=1, exportselection=False)
        entries.grid(row=0, column=0, sticky='nsew')
        horizontal = ttk.Scrollbar(rows, orient='horizontal', command=entries.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        vertical = ttk.Scrollbar(rows, orient='vertical', command=entries.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        entries.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)

        def refresh():
            entries.delete(0, 'end')
            for index, beacon in enumerate(self.beacons):
                detail = '  ·  '.join(value for value in (beacon.get('locator'), beacon.get('status')) if value)
                entries.insert('end', f'{beacon_label(beacon)}  ·  {detail}' if detail else beacon_label(beacon))
                entries.itemconfigure(index, background=beacon_row_background(beacon['hz'], self.active_theme))
            delete_all_button.state(('!disabled',) if self.beacons else ('disabled',))

        def edit():
            selection = entries.curselection()
            if selection:
                index = selection[0]
                entry = BeaconEditor(window, self, self.beacons[index]).result
                window.grab_set()
                if isinstance(entry, list):
                    self.import_vkspotter_beacons(entry)
                    refresh()
                elif entry:
                    self.beacons[index] = entry
                    changed()

        def delete():
            selection = entries.curselection()
            if selection:
                index = selection[0]
                if messagebox.askyesno('Delete beacon', f"Delete {beacon_label(self.beacons[index])}?",
                                       parent=window):
                    del self.beacons[index]
                    changed()

        def delete_all():
            count = len(self.beacons)
            if count and messagebox.askyesno(
                'Delete all beacons',
                f'Delete all {count} saved beacon {"entry" if count == 1 else "entries"}?',
                parent=window,
                default=messagebox.NO,
            ):
                self.beacons.clear()
                changed()

        def changed():
            self.refresh_beacons()
            refresh()
            self.persist_settings()

        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Edit', command=edit).pack(side='left')
        ttk.Button(buttons, text='Delete', command=delete).pack(side='left', padx=6)
        delete_all_button = ttk.Button(buttons, text='Delete All', command=delete_all)
        delete_all_button.pack(side='left')
        ttk.Button(buttons, text='Close', command=window.destroy).pack(side='right')
        entries.bind('<Double-1>', lambda _event: edit())
        refresh()
        window.update_idletasks()
        list_font = font.Font(root=window, font=entries.cget('font'))
        widest = max((list_font.measure(label) for label in entries.get(0, 'end')), default=0)
        width = min(int(window.winfo_screenwidth() * 0.9),
                    max(520, widest + vertical.winfo_reqwidth() + 42, buttons.winfo_reqwidth() + 24))
        height = min(int(window.winfo_screenheight() * 0.85),
                     max(240, entries.size() * list_font.metrics('linespace') +
                         buttons.winfo_reqheight() + horizontal.winfo_reqheight() + 46))
        window.geometry(f'{width}x{height}')
        window.deiconify()
        window.wait_visibility()
        window.grab_set()
        entries.focus_set()

    def copy_screenshot(self):
        try:
            copy_window_image(self.winfo_id())
        except (OSError, ImportError, TypeError, ValueError) as exc:
            messagebox.showwarning('Screenshot', str(exc), parent=self)
            return
        self.screenshot_button.configure(text='Copied')
        self.after(1500, lambda: self.screenshot_button.configure(text='Screenshot'))

    def change_radio(self, _event):
        model = self.model.get()
        self.port.set(self.radio_ports.get(model, ''))
        address = RADIO_ADDRESSES[model]
        if address:
            self.address.set(address)
        if not self.history:
            self.plot_model = self.model.get()
            self.draw()

    def change_window(self, _value):
        span, label, _ = TIME_WINDOWS[self.window_index.get()]
        self.window_label.set(label)
        margin = PLOT_STAGGERED_BOTTOM_MARGIN if span == HISTORY_SECONDS else PLOT_BOTTOM_MARGIN
        self.y_zoom_controls.place_configure(height=-(Y_ZOOM_TOP + margin))
        self.draw()

    def change_y_zoom(self, _value):
        self.draw()

    def zoom(self, step):
        current = self.window_index.get()
        index = max(0, min(len(TIME_WINDOWS) - 1, current + step))
        if index != current:
            self.window_index.set(index)
            self.change_window(index)

    def wheel_zoom(self, event):
        if event.delta:
            self.zoom(-1 if event.delta > 0 else 1)
        return 'break'

    def follow_live(self):
        self.view_end = None
        self.draw()

    def clear_plot(self):
        if not self.history or not messagebox.askyesno(
            'Clear Plot', 'Are you sure you want to clear the current plot?', parent=self, default=messagebox.NO,
        ):
            return
        self.plot_cleared_at = time.monotonic()
        self.history.clear()
        self.view_end = None
        if self.loaded_plot:
            self.loaded_plot = False
            self.time_offset = time.time() - self.plot_cleared_at
            self.plot_model = self.model.get()
        self.status.set('Plot cleared.')
        self.draw()

    def toggle_pause(self):
        if self.stop is None or self.stop.is_set():
            return
        if self.pause.is_set():
            self.pause_time = None
            self.pause.clear()
            self.pause_button.configure(text='Pause')
            self.status.set('Resuming meter readings…')
        else:
            self.pause.set()
            self.pause_time = time.monotonic()
            self.pause_button.configure(text='Resume')
            self.status.set('Paused · captured readings are retained.')
        self.draw()

    def latest_time(self):
        if self.pause_time is not None:
            return self.pause_time
        if self.stop is None and self.history:
            return self.history[-1][0]
        return time.monotonic()

    def scroll_history(self, action, value, unit=None):
        if not self.history:
            return
        latest = self.latest_time()
        span = TIME_WINDOWS[self.window_index.get()][0]
        oldest = self.history[0][0]
        if latest - oldest <= span:
            return
        if action == 'moveto':
            end = oldest + float(value) * (latest - oldest) + span
        else:
            current_end = latest if self.view_end is None else self.view_end
            step = span if unit == 'pages' else span / 10
            end = current_end + int(value) * step
        self.view_end = None if end >= latest - 0.01 else max(oldest + span, end)
        self.draw()

    def refresh_ports(self):
        from serial.tools.list_ports import comports

        try:
            ports = sorted(port.device for port in comports())
        except OSError as exc:
            self.status.set(f'Could not list COM ports: {exc}')
            return
        self.inputs[0]['values'] = ports
        if not self.port.get() and len(ports) == 1:
            self.port.set(ports[0])

    def reconnect_last_radio(self):
        if not self.auto_connect or self.stop is not None:
            return
        saved = self.last_connected_radio
        self.model.set(saved['model'])
        self.port.set(saved['port'])
        self.baud.set(saved['baud'])
        self.address.set(saved['address'])
        self.toggle_connection(automatic=True)

    def toggle_connection(self, automatic=False):
        if self.stop is not None:
            self.stop.set()
            self.set_radio_responding(False)
            self.connect.configure(state='disabled')
            self.pause_button.configure(state='disabled')
            self.mute_button.configure(state='disabled')
            self.status.set('Disconnecting…')
            return
        try:
            port = self.port.get().strip()
            baud = int(self.baud.get())
            address = int(self.address.get().strip(), 16)
            if not port or baud <= 0 or not 1 <= address < 0xE0:
                raise ValueError
        except ValueError:
            messagebox.showwarning('Connection settings', 'Enter a COM port, baud rate and hex CI-V address.')
            return
        if self.loaded_plot and not messagebox.askokcancel(
            'Start live plot', 'Connecting will replace the displayed plot. Continue?', parent=self,
        ):
            return
        self.history.clear()
        self.loaded_plot = False
        self.time_offset = time.time() - time.monotonic()
        self.view_end = None
        self.plot_model = self.model.get()
        self.draw()
        self.readout.set('Raw CI-V  —')
        self.stop = Event()
        self.auto_connecting = automatic
        self.radio_requests = Queue(maxsize=2)
        self.connect.configure(text='Disconnect')
        self.pause_button.configure(state='disabled' if automatic else 'normal')
        self.radio_picker.configure(state='disabled')
        for widget in (*self.inputs, self.refresh):
            widget.configure(state='disabled')
        self.status.set(f'Waiting for a meter reply on {port}…')
        Thread(
            target=sample_radio,
            args=(port, baud, address, self.stop, self.pause, self.events, self.radio_requests,
                  lambda: self.sample_interval),
            daemon=True,
        ).start()

    def toggle_radio_mute(self):
        if self.stop is None or self.stop.is_set() or self.audio_pending:
            return
        self.audio_pending = True
        self.mute_button.configure(state='disabled')
        self.radio_requests.put_nowait(('audio', self.restore_volume))

    def set_radio_responding(self, responding):
        if self.radio_responding != responding:
            self.radio_responding = responding
            self.radio_backdrop.itemconfigure('radio_indicator', fill=self.radio_indicator_colour())

    def radio_indicator_colour(self):
        return '#a6f326' if self.radio_responding else '#e93c46'

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'sample':
                    if payload[0] >= self.plot_cleared_at:
                        self.history.append(payload)
                    value = payload[1]
                    self.set_radio_responding(value is not None and self.stop is not None and not self.stop.is_set())
                    self.readout.set(f"Raw CI-V  {'—' if value is None else value}")
                    if not self.tuning and not self.pause.is_set():
                        self.status.set('No meter reply. Check the radio’s COM port, baud and address.'
                                        if value is None else f'Receiving meter readings every {self.sample_interval:g} s.')
                    if self.auto_connecting:
                        self.auto_connecting = False
                        if value is None:
                            self.toggle_connection()
                            self.status.set(f'No radio reply on {self.port.get()}. Check the radio, then click Connect.')
                            continue
                        self.pause_button.configure(state='normal')
                    if value is not None and not self.audio_pending and not self.stop.is_set():
                        self.mute_button.configure(state='normal')
                    self.remember_port(value)
                elif kind == 'frequency':
                    self.show_frequency(payload)
                elif kind == 'plot_gap':
                    if payload >= self.plot_cleared_at:
                        self.history.append((payload, None))
                elif kind in ('tuned', 'tune_failed'):
                    self.tuning = False
                    self.beacon_picker.configure(state='readonly')
                    if kind == 'tuned':
                        self.show_frequency(payload['hz'])
                        suffix = ' · paused' if self.pause.is_set() else ''
                        self.status.set(f"Tuned to {payload['callsign']} · CW / Filter 1{suffix}")
                    else:
                        self.show_frequency(None)
                        messagebox.showwarning('Tune beacon', payload, parent=self)
                elif kind in ('audio_set', 'audio_failed'):
                    self.audio_pending = False
                    if not self.stop.is_set():
                        self.mute_button.configure(state='normal')
                    if kind == 'audio_set':
                        self.restore_volume = payload
                        self.mute_button.configure(text='Unmute radio' if payload is not None else 'Mute radio')
                    else:
                        messagebox.showwarning('Radio volume', payload, parent=self)
                elif kind == 'error':
                    self.set_radio_responding(False)
                    self.status.set(f"Could not auto-connect on {self.port.get()}. {payload}"
                                    if self.auto_connecting else payload)
                    self.readout.set('Raw CI-V  —')
                elif kind == 'closed':
                    self.set_radio_responding(False)
                    if self.status.get() == 'Disconnecting…':
                        self.status.set('Disconnected.')
                    self.stop = None
                    self.restore_volume = None
                    self.audio_pending = False
                    self.mute_button.configure(text='Mute radio', state='disabled')
                    self.auto_connecting = False
                    self.pause.clear()
                    self.pause_time = None
                    self.pause_button.configure(text='Pause', state='disabled')
                    self.tuning = False
                    self.beacon_picker.configure(state='readonly')
                    self.show_frequency(None)
                    self.readout.set('Raw CI-V  —')
                    self.draw()
                    self.connect.configure(text='Connect', state='normal')
                    self.radio_picker.configure(state='readonly')
                    for widget in (*self.inputs, self.refresh):
                        widget.configure(state='normal')
        except Empty:
            pass
        if self.history and self.last_draw != self.history[-1]:
            self.draw()
        self.after(250, self.poll)

    def update_footer(self, *_args):
        if self.active_theme is None:
            return
        width, height = self.footer.winfo_width(), self.footer.winfo_height()
        colour = DISPLAY_THEMES[self.active_theme]['text']
        self.footer.delete('readouts')
        self.footer.create_text(12, height / 2, text=self.status.get(), anchor='w',
                                fill=colour, tags='readouts')
        self.footer.create_text(width - 12, height / 2, text=self.readout.get(), anchor='e',
                                fill=colour, tags='readouts')

    def draw_clocks(self):
        local, utc = clock_labels(datetime.now(timezone.utc))
        colour = '#103047' if self.active_theme == 'Day' else '#f1f7ff'
        self.backdrop.delete('clocks')
        self.backdrop.create_text(self.winfo_width() - 24, 43, text=f'{local}\n{utc}',
                                  anchor='e', justify='right', font=('Segoe UI', 10, 'bold'),
                                  fill=colour, tags='clocks')

    def choose_plot_output(self, extension, filetype):
        if not self.history:
            messagebox.showinfo('Plot files', 'There are no readings to save yet.', parent=self)
            return None
        stamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        path = filedialog.asksaveasfilename(parent=self, defaultextension=extension,
                                           initialfile=f'Beacon_Plot_{stamp}{extension}',
                                           filetypes=[(filetype, f'*{extension}')])
        return Path(path) if path else None

    def timestamped_samples(self):
        return [(timestamp + self.time_offset, raw) for timestamp, raw in self.history]

    def save_plot(self):
        path = self.choose_plot_output('.bplot', 'Beacon plot')
        if path is None:
            return
        try:
            save_settings(path, {'format': PLOT_FORMAT, 'radio_model': self.plot_model,
                                 'samples': self.timestamped_samples()})
        except OSError as exc:
            messagebox.showwarning('Save plot', str(exc), parent=self)

    def open_plot(self):
        if self.stop is not None:
            messagebox.showinfo('Open saved plot', 'Disconnect the radio before opening a saved plot.', parent=self)
            return
        path = filedialog.askopenfilename(parent=self, filetypes=[('Beacon plot', '*.bplot')])
        if not path:
            return
        try:
            model, history = read_plot(Path(path))
        except (OSError, ValueError, RecursionError) as exc:
            messagebox.showwarning('Open saved plot', str(exc), parent=self)
            return
        if self.history and not messagebox.askokcancel(
            'Open saved plot', 'Replace the currently displayed plot?', parent=self,
        ):
            return
        self.history = history
        self.loaded_plot = True
        self.time_offset = 0
        self.plot_model = model
        self.view_end = None
        duration = history[-1][0] - history[0][0]
        index = next((i for i, (span, _, _) in enumerate(TIME_WINDOWS) if span >= duration),
                     len(TIME_WINDOWS) - 1)
        self.window_index.set(index)
        self.status.set(f'Saved plot · {Path(path).name}')
        self.change_window(index)

    def export_csv(self):
        path = self.choose_plot_output('.csv', 'CSV files')
        if path is None:
            return
        try:
            write_plot_csv(path, self.timestamped_samples())
        except (OSError, ValueError, OverflowError) as exc:
            messagebox.showwarning('Export CSV', str(exc), parent=self)

    def open_setup(self):
        try:
            result = SetupDialog(self).result
        finally:
            self.plot_transparency_preview = None
            self.display_mode_preview = None
            self.setup_dialog = None
        if result is not None:
            (self.plot_transparency, self.warn_on_close, self.auto_connect,
             units, reference, mode, smoothing, self.station_callsign, self.station_locator,
             self.station_coordinates, self.sample_interval, synoptic_source, self.hepburn_source) = result
            if synoptic_source != self.synoptic_source:
                self.synoptic_source = synoptic_source
                if self.synoptic_window is not None and self.synoptic_window.winfo_exists():
                    self.synoptic_window.destroy()
            self.axis_units.set(units)
            self.dbm_reference.set(reference)
            self.display_theme.set(mode)
            self.smoothing.set(smoothing)
            self.sync_beacon()
            self.persist_settings()
        self.apply_display_theme()
        self.render_plot_wallpaper()
        self.draw()

    def render_plot_wallpaper(self):
        chart = self.chart
        if self.plot_wallpaper_layers is None:
            chart.delete('wallpaper')
            chart.wallpaper_photo = None
            return

        from PIL import Image, ImageTk

        landscape, tint = self.plot_wallpaper_layers
        percent = self.plot_transparency_preview
        if percent is None:
            percent = self.plot_transparency
        photo = ImageTk.PhotoImage(Image.blend(landscape, tint, (100 - percent) / 100), master=chart)
        if chart.find_withtag('wallpaper'):
            chart.itemconfigure('wallpaper', image=photo)
        else:
            chart.create_image(0, 0, image=photo, anchor='nw', tags='wallpaper')
            chart.tag_lower('wallpaper')
        chart.wallpaper_photo = photo

    def schedule_backdrop_resize(self, event):
        if event.widget not in (self, self.chart) and not any(
            event.widget is container for container, _, _, _ in self.setup_panels
        ):
            return
        if self.backdrop_resize_job is not None:
            self.after_cancel(self.backdrop_resize_job)
        self.backdrop_resize_job = self.after(60, self.update_backdrop)

    def update_backdrop(self):
        if self.backdrop_resize_job is not None:
            self.after_cancel(self.backdrop_resize_job)
            self.backdrop_resize_job = None
        theme = self.active_theme
        width, height = self.winfo_width(), self.winfo_height()
        panels = (*self.setup_panels, (self.chart, self.chart, None, ()))
        bounds = [(container.winfo_x(), container.winfo_y(), container.winfo_width(), container.winfo_height())
                  for container, _, _, _ in panels]
        key = (theme, width, height, bounds)
        if key == self.backdrop_key or min(width, height) < 2:
            return
        self.backdrop_key = key
        palette = DISPLAY_THEMES[theme]
        heading_drawn = False
        scene = None
        heading_colour = '#103047' if theme == 'Day' else '#f1f7ff'
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

            asset = Path(__file__).resolve().with_name(f'beacon_background_{theme.lower()}.png')
            with Image.open(asset) as source:
                scene = ImageOps.fit(source.convert('RGB'), (width, height), Image.Resampling.LANCZOS)
            try:
                font_file = asset.with_name('OpenSans-Bold.ttf')
                font = ImageFont.truetype(str(font_file), size=44)
            except OSError:
                pass
            else:
                draw = ImageDraw.Draw(scene)
                _, top, _, bottom = draw.textbbox((0, 0), PLOTTER_HEADING, font=font)
                draw.text((24, 45 - (top + bottom) / 2), PLOTTER_HEADING, font=font,
                          fill=heading_colour)
                heading_drawn = True
            self.backdrop_photo = ImageTk.PhotoImage(scene, master=self)
        except (ImportError, OSError, tk.TclError):
            self.backdrop_photo = None
            heading_drawn = False
        self.backdrop.configure(background=palette['background'])
        self.backdrop.delete('all')
        if self.backdrop_photo is not None:
            self.backdrop.create_image(0, 0, image=self.backdrop_photo, anchor='nw')
        if not heading_drawn:
            self.backdrop.create_text(24, 45, text=PLOTTER_HEADING, anchor='w',
                                      font=('Segoe UI', 25, 'bold'), fill=heading_colour)

        self.plot_wallpaper_layers = None
        for (_, panel, title, labels), (x, y, panel_width, panel_height) in zip(panels, bounds):
            is_plot = panel is self.chart
            base = palette['panel'] if is_plot else palette['background']
            panel.configure(background=base)
            panel.wallpaper_photo = None
            if scene is not None and panel_width > 1 and panel_height > 1:
                crop = scene.crop((x, y, x + panel_width, y + panel_height))
                tint = Image.new('RGB', crop.size, base)
                if is_plot:
                    self.plot_wallpaper_layers = (crop, tint)
                else:
                    panel.wallpaper_photo = ImageTk.PhotoImage(Image.blend(crop, tint, 0.5), master=panel)
            if is_plot:
                continue
            panel.delete('all')
            if panel.wallpaper_photo is not None:
                panel.create_image(0, 0, image=panel.wallpaper_photo, anchor='nw')
            panel.create_rectangle(0, 0, panel_width - 1, panel_height - 1, outline=palette['border'])
            if title is not None:
                title_x = 10
                if panel is self.radio_backdrop:
                    panel.create_oval(10, 6, 20, 16, fill=self.radio_indicator_colour(),
                                      outline=palette['border'], tags='radio_indicator')
                    title_x = 26
                panel.create_text(title_x, 11, text=title, anchor='w', fill=palette['text'],
                                  font=('Segoe UI', 9, 'bold'))
            for label, widget in labels:
                panel.create_text(widget.winfo_x(), 29, text=label, anchor='w', fill=palette['text'])

        self.update_footer()
        self.draw_clocks()
        self.render_plot_wallpaper()
        self.draw()

    def draw(self):
        theme = self.active_theme
        palette = DISPLAY_THEMES[theme]
        trace_colours = SIGNAL_COLOURS[theme]
        canvas = self.chart
        canvas.delete('all')
        if canvas.wallpaper_photo is not None:
            canvas.create_image(0, 0, image=canvas.wallpaper_photo, anchor='nw', tags='wallpaper')
        latest = self.latest_time()
        while not self.loaded_plot and self.history and latest - self.history[0][0] > HISTORY_SECONDS:
            self.history.popleft()
        self.clear_plot_button.configure(state='normal' if self.history else 'disabled')
        self.last_draw = self.history[-1] if self.history else None
        span, _, interval = TIME_WINDOWS[self.window_index.get()]
        oldest = self.history[0][0] if self.history else latest
        available = latest - oldest
        if available <= span:
            self.view_end = None
            self.history_scroll.set(0, 1)
        else:
            if self.view_end is not None:
                self.view_end = max(oldest + span, min(self.view_end, latest))
                if self.view_end >= latest:
                    self.view_end = None
            end = latest if self.view_end is None else self.view_end
            self.history_scroll.set(max(0, (end - span - oldest) / available), (end - oldest) / available)
        self.live_button.configure(state='disabled' if self.view_end is None else 'normal')
        now = latest if self.view_end is None else self.view_end
        stagger = span == HISTORY_SECONDS
        axis_end = now + self.time_offset
        left, top = 70, 27
        bottom_margin = PLOT_STAGGERED_BOTTOM_MARGIN if stagger else PLOT_BOTTOM_MARGIN
        right = max(left + 1, canvas.winfo_width() - 64)
        bottom = max(top + 1, canvas.winfo_height() - bottom_margin)

        positions = IC9700_POSITIONS if self.plot_model == 'IC-9700' else STANDARD_POSITIONS
        ticks = METER_TICKS
        reference = None
        axis_title = 'Approx. signal strength  ·  dB above S9'
        if self.axis_units.get() == 'dBm':
            reference = DBM_REFERENCES[self.dbm_reference.get()]
            ticks = dbm_ticks(reference)
            axis_title = f'Estimated signal strength (dBm) · S9 = {reference} dBm'
        elif self.plot_model == 'Custom Icom':
            positions, ticks = RAW_POSITIONS, RAW_TICKS
            axis_title = 'Raw CI-V meter level'

        zoom = self.y_zoom.get()
        ceiling = positions[-1] / zoom
        if zoom > 1:
            ticks = zoomed_meter_ticks(ceiling, raw=positions is RAW_POSITIONS, reference=reference)

        def y(position):
            return bottom - position / ceiling * (bottom - top)

        for position, label in ticks:
            height = y(position)
            canvas.create_line(left, height, right, height, fill=palette['reference'] if positions is not RAW_POSITIONS and position == 54 else palette['grid'])
            canvas.create_text(left - 7, height, text=label, anchor='e', fill=palette['text'])
        for index, (timestamp, label) in enumerate(clock_ticks(axis_end, span, interval)):
            x = right - (axis_end - timestamp) / span * (right - left)
            canvas.create_line(x, top, x, bottom, fill=palette['grid'])
            label_x = min(x, canvas.winfo_width() - 22)
            row = index % 2 if stagger else 0
            canvas.create_text(label_x, bottom + 12 + row * 14, text=label, fill=palette['text'])
        canvas.create_text(left, 12, text=axis_title, anchor='w', fill=palette['text'])
        columns = {}
        visible = []
        count = SMOOTHING_WINDOWS[self.smoothing.get()]
        warmup = count - 1
        for timestamp, raw in reversed(self.history):
            if timestamp > now:
                continue
            if timestamp < now - span:
                if warmup == 0:
                    break
                warmup -= 1
            visible.append((timestamp, raw))
        for timestamp, raw in smooth_samples(reversed(visible), count):
            if timestamp < now - span:
                continue
            column = round(right - (now - timestamp) / span * (right - left))
            if raw is None:
                columns.setdefault(column, []).append(None)
                continue
            segments = columns.setdefault(column, [])
            if not segments or segments[-1] is None:
                segments.append([raw, raw, raw, raw])
            else:
                limits = segments[-1]
                limits[1] = min(limits[1], raw)
                limits[2] = max(limits[2], raw)
                limits[3] = raw

        previous = None
        for column, segments in columns.items():
            for segment in segments:
                if segment is None:
                    previous = None
                    continue
                first, low, high, last = segment
                for raw in (first, low, high, last):
                    point = (column, y(positions[raw]), raw)
                    if point == previous:
                        continue
                    if previous is None and point[1] >= top:
                        x, height, _ = point
                        canvas.create_oval(x - 1, height - 1, x + 1, height + 1,
                                           fill=trace_colours[raw], outline='')
                    elif previous is not None and (segment_on_screen := visible_trace_segment(previous, point, top)):
                        colour = trace_colours[round((previous[2] + raw) / 2)]
                        canvas.create_line(*segment_on_screen, fill=colour, width=2)
                    previous = point

    def remember_port(self, value):
        if value is None:
            return
        port = self.port.get().strip()
        connection = {'model': self.plot_model, 'port': port, 'baud': self.baud.get(),
                      'address': self.address.get().strip()}
        if self.last_connected_radio != connection or self.radio_ports.get(self.plot_model) != port:
            self.last_connected_radio = connection
            self.radio_ports[self.plot_model] = port
            self.persist_settings()

    def persist_settings(self):
        try:
            save_settings(self.settings_path, {
                'model': self.model.get(),
                'display_theme': self.display_theme.get(),
                'radio_ports': self.radio_ports,
                'last_connected_radio': self.last_connected_radio,
                'beacons': self.beacons,
                'baud': self.baud.get(),
                'address': self.address.get(),
                'sample_interval_seconds': self.sample_interval,
                'smoothing': self.smoothing.get(),
                'axis_units': self.axis_units.get(),
                'dbm_reference': self.dbm_reference.get(),
                'plot_transparency': self.plot_transparency,
                'warn_on_close': self.warn_on_close,
                'auto_connect': self.auto_connect,
                'station_callsign': self.station_callsign,
                'station_locator': self.station_locator,
                'google_maps_key': self.google_maps_key,
                'synoptic_source': self.synoptic_source,
                'hepburn_source': self.hepburn_source,
            })
        except OSError as exc:
            messagebox.showwarning('Plotter settings', f'Could not save settings. {exc}', parent=self)

    def close(self):
        if self.warn_on_close and not messagebox.askokcancel(
            'Close plotter', 'Closing the app will discard the current plot. Close anyway?',
            default=messagebox.CANCEL, parent=self,
        ):
            return
        if self.stop is not None:
            self.stop.set()
        if self.map_server is not None:
            self.map_server.close()
        self.persist_settings()
        self.destroy()


if __name__ == '__main__':
    Plotter().mainloop()
