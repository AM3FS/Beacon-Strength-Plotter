"""Standalone receive S-meter history for Icom and Yaesu radios."""
from bisect import bisect_left, bisect_right
from collections import deque
import csv
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from functools import partial
from html.parser import HTMLParser
from io import BytesIO
import json
from math import ceil
import os
import re
import sys
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
import time
import tkinter as tk
from tkinter import filedialog, font, messagebox, simpledialog, ttk
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import webbrowser

from beacon_map import MAP_ORIGIN, MY_MAPS_URL, BeaconMapServer, map_page, path_profile_url, write_path_kml
from plot_tools import DailyRecording, LiftDetector, alert_preferences, align_recording, utc_day
from station_location import beacon_path, country_at, maidenhead_coordinates
from updates import APP_VERSION, check_latest_release
from vkspotter import BEACONS_URL, clipboard_beacons, fetch_beacons, listed_frequency_hz
from vkspotter_credentials import forget_login, read_login, save_login
from yaesu_cat import YAESU_MODELS, YaesuCAT, open_cat_port, supports_frequency

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
RADIO_MODELS = (*RADIO_ADDRESSES, *YAESU_MODELS)
DEFAULT_RADIO = 'IC-9700'
DEFAULT_SYNOPTIC_CHART = 'https://reg.bom.gov.au/fwo/IDY00030.202611211800.png'
BOM_SYNOPTIC_PAGE = 'https://reg.bom.gov.au/australia/charts/synoptic_col.shtml'
GOOGLE_CLOUD_CONSOLE_URL = 'https://console.cloud.google.com/'
HEPBURN_CHART_INDEX = 'https://www.dxinfocentre.com/tropo.html'
ADD_BEACON = 'Add New Beacon'
DEFAULT_BEACONS = ({'callsign': 'VK5RSE', 'location': 'Mount Gambier', 'hz': 145_550_000},)
SAMPLE_INTERVALS = (0.5, *range(1, 11))
AUTO_CONNECT_INTERVAL = 5
AUTO_CONNECT_WINDOW = 60
TIME_WINDOWS = (
    (300, '5 min', 5), (600, '10 min', 5), (900, '15 min', 5), (1800, '30 min', 5),
    (3600, '1 hr', 10), (7200, '2 hr', 15), (14400, '4 hr', 30), (28800, '8 hr', 60),
    (43200, '12 hr', 60), (86400, '24 hr', 60),
)
HISTORY_SECONDS = TIME_WINDOWS[-1][0]
LONG_VIEW_REDRAW_INTERVAL = 1.0
PLOT_BOTTOM_MARGIN = 34
PLOT_STAGGERED_BOTTOM_MARGIN = 44
Y_ZOOM_TOP = 14
MIN_WINDOW_WIDTH = 780
MIN_WINDOW_HEIGHT = 470


def validate_chart_url(source):
    parsed = urlsplit(source)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Enter a complete HTTP or HTTPS URL.')


class BoMChartParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image_url = ''

    def handle_starttag(self, tag, attributes):
        if tag != 'img' or self.image_url:
            return
        source = dict(attributes).get('src')
        if source:
            url = urljoin(BOM_SYNOPTIC_PAGE, source)
            parsed = urlsplit(url)
            if parsed.scheme == 'https' and parsed.netloc == 'reg.bom.gov.au' and re.fullmatch(
                r'/fwo/IDY00030\.[0-9]{12}\.png', parsed.path,
            ):
                self.image_url = url


def latest_bom_chart_url():
    # The displayed image is the current analysis. The page also has an archive in its script.
    request = Request(BOM_SYNOPTIC_PAGE, headers={'Cache-Control': 'no-cache'})
    max_bytes = 2 * 1024 * 1024
    with urlopen(request, timeout=12) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('The BoM chart page is too large.')
    parser = BoMChartParser()
    parser.feed(data.decode('utf-8', errors='replace'))
    parser.close()
    if not parser.image_url:
        raise ValueError('No current synoptic chart was found on the BoM page.')
    return parser.image_url


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
TRACE_BAND_COLOURS = {
    '6m': ('#865b00', '#f2c45d'),
    '2m': ('#194187', '#3bcbef'),
    '70cm': ('#14643a', '#5bdd8b'),
    '23cm': ('#7339a9', '#ce9bff'),
    '13cm': ('#a64c00', '#ff992c'),
    '9cm': ('#086c68', '#5adbd0'),
    '6cm': ('#a73569', '#ff91bd'),
    '3cm': ('#576812', '#bad768'),
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
LEGACY_PLOT_FORMAT = 'vk3fs-beacon-plot-v1'
FREQUENCY_PLOT_FORMAT = 'vk3fs-beacon-plot-v2'
PLOT_FORMAT = 'vk3fs-beacon-plot-v3'
PLOT_COMMENT_LIMIT = 120


def smooth_samples(samples, count):
    if count == 1:
        yield from samples
        return
    recent = deque(maxlen=count)
    total = 0
    previous_hz = None
    for timestamp, raw, hz, callsign in samples:
        if raw is None or hz != previous_hz:
            recent.clear()
            total = 0
        previous_hz = hz
        if raw is None:
            yield timestamp, None, hz, callsign
        else:
            if len(recent) == count:
                total -= recent[0]
            recent.append(raw)
            total += raw
            yield timestamp, round(total / len(recent)), hz, callsign


def read_plot(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    formats = {LEGACY_PLOT_FORMAT: 2, FREQUENCY_PLOT_FORMAT: 3, PLOT_FORMAT: 4}
    if not isinstance(data, dict) or not isinstance(data.get('format'), str) or data['format'] not in formats:
        raise ValueError('Choose a plot file saved by Beacon Strength Plotter.')
    reading_size = formats[data['format']]
    model = data.get('radio_model')
    samples = data.get('samples')
    if not isinstance(model, str) or model not in RADIO_MODELS or not isinstance(samples, list) or not samples:
        raise ValueError('The plot file is missing its radio model or readings.')
    history = deque()
    previous = 0
    for point in samples:
        if not isinstance(point, list) or len(point) != reading_size:
            raise ValueError('The plot file contains an invalid reading.')
        stamp, raw = point[:2]
        hz = point[2] if reading_size >= 3 else None
        callsign = point[3] if reading_size == 4 else None
        if (type(stamp) not in (int, float) or not previous <= stamp < 253402214400
                or (raw is not None and (type(raw) is not int or not 0 <= raw <= 255))):
            raise ValueError('The plot file contains an invalid timestamp or meter reading.')
        if hz is not None and (type(hz) is not int or not 0 < hz < 10**12):
            raise ValueError('The plot file contains an invalid frequency.')
        if callsign is not None and (not isinstance(callsign, str) or not callsign.isprintable()):
            raise ValueError('The plot file contains an invalid beacon callsign.')
        history.append((stamp, raw, hz, callsign))
        previous = stamp
    stored_comments = data.get('comments', [])
    comments = {}
    if not isinstance(stored_comments, list):
        raise ValueError('The plot file contains invalid comments.')
    for item in stored_comments:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError('The plot file contains an invalid comment.')
        stamp, text = item
        if (type(stamp) not in (int, float) or not 0 <= stamp < 253402214400
                or not isinstance(text, str) or not text.isprintable() or not text.strip()
                or len(text) > PLOT_COMMENT_LIMIT):
            raise ValueError('The plot file contains an invalid comment.')
        comments[stamp] = text
    return model, history, comments


def write_plot_csv(path, samples, model=DEFAULT_RADIO):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(('timestamp_utc', 'raw_cat' if model in YAESU_MODELS else 'raw_ci_v'))
        for timestamp, raw, _, _callsign in samples:
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
        self.show_signal_guide = tk.BooleanVar(parent, value=parent.show_signal_guide)
        self.dbm_reference = tk.StringVar(parent, value=parent.dbm_reference.get())
        self.display_mode = tk.StringVar(parent, value=parent.display_theme.get())
        self.smoothing = tk.StringVar(parent, value=parent.smoothing.get())
        self.sample_interval = tk.StringVar(parent, value=f'{parent.sample_interval:g} s')
        self.callsign = tk.StringVar(parent, value=parent.station_callsign)
        self.locator = tk.StringVar(parent, value=parent.station_locator)
        self.synoptic_source = tk.StringVar(parent, value=parent.synoptic_source)
        self.synoptic_auto_bom = tk.BooleanVar(parent, value=parent.synoptic_auto_bom)
        self.manual_synoptic_source = parent.synoptic_source
        self.bom_chart_url = parent.bom_chart_url
        self.bom_fetch_job = None
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
        self.link_font = font.nametofont('TkDefaultFont', root=self).copy()
        self.link_font.configure(underline=True)
        self.reference_link = None
        raw_cat = self.parent.plot_model in YAESU_MODELS
        if raw_cat:
            ttk.Label(choices, text='Yaesu plots use raw CAT meter counts (0–255).').pack(anchor='w')
        else:
            for units in AXIS_UNITS:
                ttk.Radiobutton(choices, text=units, value=units, variable=self.axis_units,
                                command=self.update_reference_state).pack(side='left', padx=(0, 8))
            reference = ttk.Frame(axis)
            reference.pack(fill='x', pady=(5, 0))
            ttk.Label(reference, text='S9 reference').pack(side='left', padx=(0, 6))
            self.reference_picker = ttk.Combobox(reference, textvariable=self.dbm_reference,
                                                 values=tuple(DBM_REFERENCES), width=19)
            self.reference_picker.pack(side='left')
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
        ttk.Checkbutton(axis, text='Show latest signal level and guide',
                        variable=self.show_signal_guide).pack(anchor='w', pady=(5, 0))

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
        ttk.Checkbutton(options, text='Radio auto-connect', variable=self.auto_connect).pack(side='left')
        self.update_button = ttk.Button(options, text='Check for updates…', command=self.check_for_updates)
        self.update_button.pack(side='right')
        synoptic = ttk.LabelFrame(frame, padding=8)
        synoptic.pack(fill='x', padx=10, pady=(0, 8))
        synoptic_title = ttk.Frame(synoptic)
        ttk.Label(synoptic_title, text='Synoptic chart').pack(side='left')
        ttk.Checkbutton(synoptic_title, text='Auto fetch BoM', variable=self.synoptic_auto_bom,
                        command=self.change_synoptic_source).pack(side='left', padx=(12, 0))
        synoptic.configure(labelwidget=synoptic_title)
        synoptic_details = ttk.Frame(synoptic)
        synoptic_details.pack(fill='x', pady=(0, 4))
        ttk.Label(synoptic_details, text='Image URL').pack(side='left')
        self.bom_status = ttk.Label(synoptic_details)
        self.bom_status.pack(side='right')
        self.synoptic_entry = ttk.Entry(synoptic, textvariable=self.synoptic_source, width=34)
        self.synoptic_entry.pack(fill='x')
        if self.synoptic_auto_bom.get():
            self.change_synoptic_source()

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
        if self.reference_link is not None:
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

    def change_synoptic_source(self):
        if self.bom_fetch_job is not None:
            self.after_cancel(self.bom_fetch_job)
            self.bom_fetch_job = None
        if not self.synoptic_auto_bom.get():
            self.synoptic_entry.state(['!readonly'])
            self.synoptic_source.set(self.manual_synoptic_source)
            self.bom_status.configure(text='')
            return

        self.manual_synoptic_source = self.synoptic_source.get()
        self.synoptic_entry.state(['readonly'])
        self.synoptic_source.set(self.bom_chart_url)
        self.bom_status.configure(text='Fetching BoM chart…')
        result = Queue(maxsize=1)

        def fetch():
            try:
                result.put(latest_bom_chart_url())
            except (OSError, ValueError):
                result.put('')

        def show_result():
            self.bom_fetch_job = None
            try:
                url = result.get_nowait()
            except Empty:
                self.bom_fetch_job = self.after(100, show_result)
                return
            if url:
                self.bom_chart_url = url
                self.synoptic_source.set(url)
            self.bom_status.configure(text='' if url else 'Could not refresh BoM link')

        Thread(target=fetch, daemon=True).start()
        self.bom_fetch_job = self.after(100, show_result)

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
        for title, source, entry in (
            ('Synoptic chart', '' if self.synoptic_auto_bom.get() else self.synoptic_source.get(),
             self.synoptic_entry),
            ('Hepburn Chart', self.hepburn_source.get(), self.hepburn_entry),
        ):
            try:
                if source := source.strip():
                    validate_chart_url(source)
            except ValueError as exc:
                messagebox.showwarning(title, str(exc), parent=self)
                entry.focus_set()
                return False
        return True

    def apply(self):
        auto_bom = self.synoptic_auto_bom.get()
        manual_source = self.manual_synoptic_source if auto_bom else self.synoptic_source.get()
        self.result = (self.percent.get(), self.warn.get(), self.auto_connect.get(),
                       self.axis_units.get(), self.dbm_reference.get(), self.display_mode.get(), self.smoothing.get(),
                       self.callsign.get().strip().upper(), self.locator.get().strip().upper(),
                       self.station_coordinates, float(self.sample_interval.get().split()[0]),
                       manual_source.strip(), auto_bom, self.bom_chart_url, self.hepburn_source.get().strip(),
                       self.show_signal_guide.get())

    def destroy(self):
        self.locator.trace_remove('write', self.locator_trace)
        if self.update_check_job is not None:
            self.after_cancel(self.update_check_job)
        if self.bom_fetch_job is not None:
            self.after_cancel(self.bom_fetch_job)
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


def signal_level_label(position, reference, *, raw_scale):
    if raw_scale:
        return str(position)
    if reference is not None:
        return f'{reference + position - 54:.0f} dBm'
    if position <= 54:
        s_units = f'{position / 6:.1f}'.removesuffix('.0')
        return f'S{s_units}'
    return f'S9+{position - 54:.0f} dB'


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


def draw_trace(canvas, points, top, *, width=2):
    # Send contiguous sections of the same colour to Tk as one canvas item.
    # Limit path size so long traces remain safe for Windows Tk drawing.
    path = []
    previous = None
    stroke = None
    for point, colour in points:
        if previous is not None and point == previous:
            continue
        segment = visible_trace_segment(previous, point, top) if previous and point else None
        if path and (segment is None or colour != stroke or (path[-2] != segment[0] or path[-1] != segment[1])
                     or len(path) >= 1024):
            canvas.create_line(*path, fill=stroke, width=width, joinstyle='round')
            path.clear()
        if segment is not None:
            if not path:
                path.extend(segment[:2])
                stroke = colour
            path.extend(segment[2:])
        elif previous is None and point is not None and point[1] >= top:
            x, height = point[:2]
            canvas.create_oval(x - 1, height - 1, x + 1, height + 1, fill=colour, outline='')
        previous = point
    if path:
        canvas.create_line(*path, fill=stroke, width=width, joinstyle='round')


def radio_setup(settings):
    model = settings.get('model', DEFAULT_RADIO)
    if not isinstance(model, str) or model not in RADIO_MODELS:
        model = DEFAULT_RADIO
    return model, settings.get('address', RADIO_ADDRESSES.get(model, ''))



def startup_radio(settings, model, ports):
    saved = settings.get('last_connected_radio')
    if saved is None and ports.get(model):
        # Earlier versions only saved a per-model port after a valid meter reply.
        saved = {'model': model, 'port': ports[model], 'baud': settings.get('baud', '115200'),
                 'address': settings.get('address', RADIO_ADDRESSES.get(model, ''))}
    if not isinstance(saved, dict):
        return None
    try:
        if saved['model'] not in RADIO_MODELS or not isinstance(saved['port'], str) or not saved['port'].strip():
            return None
        if int(saved['baud']) <= 0:
            return None
        if saved['model'] in YAESU_MODELS:
            return {**saved, 'address': ''}
        if not 1 <= int(saved['address'], 16) < 0xE0:
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


def windows_com_device_names():
    # Some virtual ports have a DOS device name but are omitted by pyserial's device scan.
    query = ctypes.WinDLL('kernel32', use_last_error=True).QueryDosDeviceW
    query.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    query.restype = wintypes.DWORD
    buffer_size = 4096
    while True:
        buffer = ctypes.create_unicode_buffer(buffer_size)
        count = query(None, buffer, buffer_size)
        if count:
            return {name.upper() for name in buffer[:count].split('\0')
                    if re.fullmatch(r'COM[0-9]+', name, re.IGNORECASE)}
        error = ctypes.get_last_error()
        if error != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(error)
        buffer_size *= 2


def available_serial_ports():
    from serial.tools.list_ports import comports

    if os.name != 'nt':
        return sorted({port.device for port in comports()})

    try:
        ports = {port.device.upper() for port in comports()}
    except OSError:
        ports = None
    try:
        windows_ports = windows_com_device_names()
    except OSError:
        if ports is None:
            raise
        windows_ports = set()

    def sort_key(name):
        number = name[3:] if name.startswith('COM') else ''
        return (int(number) if number.isdecimal() else float('inf'), name)

    return sorted((ports or set()) | windows_ports, key=sort_key)


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
        self.use_record = ttk.Button(buttons, text='Use Selected', command=self.ok, state='disabled')
        self.use_record.pack(side='right', padx=6)
        self.use_all = ttk.Button(buttons, text='Add All', command=self.add_all_shown, state='disabled')
        self.use_all.pack(side='right')
        self.forget = ttk.Button(buttons, text='Forget Saved Login', command=self.clear_saved_login,
                                 state='normal' if self.saved_login else 'disabled')
        self.forget.pack(side='left')
        self.bind('<Escape>', self.cancel)
        self.table.bind('<Return>', self.ok)
        self.table.bind('<<TreeviewSelect>>', self.update_import_controls)
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

    def update_import_controls(self, _event=None):
        selected = [self.records[int(item)] for item in self.table.selection()]
        can_import = bool(selected) and (all(row['hz'] is not None for row in selected)
                                         or (self.lookup and len(selected) == 1))
        ready = self.poll_job is None
        self.use_record.state(('!disabled',) if ready and can_import else ('disabled',))
        usable = sum(self.records[int(item)]['hz'] is not None for item in self.table.get_children())
        self.use_all.state(('!disabled',) if ready and usable > 1 else ('disabled',))

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
            self.update_import_controls()
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
        self.update_import_controls()
        if self.records:
            count = len(shown)
            detail = ' Ctrl+click or Shift+click to select multiple frequencies.' if usable > 1 else ''
            if usable > 1:
                detail += ' Add All imports only shown rows with a readable frequency.'
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
    return (int(digits) or None) if digits.isdecimal() else None


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


def sample_radio(port_name, baud, address, stop, pause, events, radio_requests, sample_interval,
                 model=DEFAULT_RADIO):
    import serial

    port_failed = False
    try:
        yaesu = model in YAESU_MODELS
        connection = (open_cat_port(serial, port_name, baud) if yaesu else
                      serial.Serial(port_name, baud, timeout=0.1, write_timeout=0.5))
        with connection as port:
            pending = bytearray()
            if yaesu:
                cat = YaesuCAT(port, model)
                meter_reader, frequency_reader, tuner = cat.read_meter, cat.read_frequency, cat.tune_beacon
            else:
                meter_reader = partial(read_meter, port, address, pending)
                frequency_reader = partial(read_frequency, port, address, pending)
                tuner = partial(tune_beacon, port, address, pending)
            next_frequency = 0.0
            frequency = None
            last_sample = float("-inf")
            meter_available = False
            was_paused = False
            while not stop.is_set():
                started = time.monotonic()
                try:
                    kind, argument = radio_requests.get_nowait()
                except Empty:
                    kind, argument = None, None
                if kind == 'audio' and not yaesu:
                    try:
                        restore_level = set_radio_mute(port, address, pending, argument)
                    except ValueError as exc:
                        events.put(('audio_failed', str(exc)))
                    else:
                        events.put(('audio_set', restore_level))
                if kind == 'scan_beacon':
                    cancelled, argument = argument
                    if cancelled.is_set():
                        events.put(('tune_cancelled', None))
                        continue
                    kind = 'beacon'
                if kind == 'beacon':
                    beacon = argument
                    events.put(('plot_gap', time.monotonic()))
                    try:
                        tuner(beacon['hz'])
                    except ValueError as exc:
                        frequency = None
                        next_frequency = 0.0
                        events.put(('tune_failed', str(exc)))
                    else:
                        frequency = beacon['hz']
                        next_frequency = time.monotonic() + 1.0
                        events.put(('tuned', beacon))
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
                sample = None
                if started - last_sample >= sample_interval():
                    value = meter_reader()
                    if pause.is_set():
                        was_paused = True
                        continue
                    last_sample = started
                    meter_available = value is not None
                    sample = time.monotonic(), value
                if started >= next_frequency and not stop.is_set() and not pause.is_set():
                    frequency = None
                    if meter_available:
                        try:
                            frequency = frequency_reader()
                        except ValueError:
                            pass
                    events.put(('frequency', frequency))
                    next_frequency = started + 1.0
                if sample is not None:
                    events.put(('sample', (*sample, frequency if sample[1] is not None else None)))
                stop.wait(min(0.1, max(0, last_sample + sample_interval() - time.monotonic())))
    except (OSError, serial.SerialException) as exc:
        port_failed = True
        events.put(('error', str(exc)))
    except (ValueError, OverflowError) as exc:
        events.put(('error', str(exc)))
    finally:
        events.put(('closed', port_failed))


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


class FrequencyDialog(simpledialog.Dialog):
    def __init__(self, parent, choices, title):
        self.choices = choices
        super().__init__(parent, title)

    def body(self, frame):
        self.picker = ttk.Combobox(frame, values=list(self.choices), state='readonly', width=36)
        self.picker.current(0)
        self.picker.pack(padx=12, pady=10)
        return self.picker

    def apply(self):
        self.result = (self.choices[self.picker.get()],)


class ScanDialog(simpledialog.Dialog):
    def __init__(self, parent, beacons, selected, dwell):
        self.beacons, self.selected, self.dwell = beacons, selected, dwell
        super().__init__(parent, 'Scan beacons')

    def body(self, frame):
        ttk.Label(frame, text='Select two or more frequencies. Use Ctrl-click to select individual entries.').pack(
            anchor='w', padx=10, pady=(6, 4))
        rows = ttk.Frame(frame)
        rows.pack(fill='both', expand=True, padx=10)
        self.list = tk.Listbox(rows, selectmode='extended', exportselection=False, width=66, height=9)
        self.list.pack(side='left', fill='both', expand=True)
        for index, (hz, beacon) in enumerate(self.beacons.items()):
            self.list.insert('end', beacon_label(beacon))
            if hz in self.selected:
                self.list.selection_set(index)
        bar = ttk.Scrollbar(rows, command=self.list.yview)
        bar.pack(side='right', fill='y')
        self.list.configure(yscrollcommand=bar.set)
        ttk.Label(frame, text='Dwell time per frequency (seconds)').pack(anchor='w', padx=10, pady=(8, 2))
        self.entry = ttk.Entry(frame, width=8)
        self.entry.insert(0, str(self.dwell))
        self.entry.pack(anchor='w', padx=10)
        return self.list

    def validate(self):
        selected = [list(self.beacons.values())[index] for index in self.list.curselection()]
        try:
            dwell = int(self.entry.get())
            valid = 5 <= dwell <= 3600 and len(selected) >= 2
        except ValueError:
            valid = False
        if not valid:
            messagebox.showinfo(self.title(), 'Choose at least two frequencies and a dwell time of 5–3600 seconds.',
                                parent=self)
            return False
        self.result = selected, dwell
        return True


class LiftAlertDialog(simpledialog.Dialog):
    def __init__(self, parent, beacon, rule):
        self.beacon = beacon
        self.rule = rule
        super().__init__(parent, 'Beacon lift alert')

    def body(self, frame):
        ttk.Label(frame, text=beacon_label(self.beacon)).grid(row=0, columnspan=2, padx=10, pady=8)
        self.enabled = tk.BooleanVar(self, value=self.rule is not None)
        ttk.Checkbutton(frame, text='Enable audible alert', variable=self.enabled).grid(
            row=1, columnspan=2, sticky='w', padx=10)
        threshold, duration = self.rule or (-110, 30)
        self.entries = []
        for row, (label, value) in enumerate((('Estimated dBm threshold', threshold),
                                              ('Seconds at or above threshold', duration)), start=2):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=10, pady=4)
            entry = ttk.Entry(frame, width=8)
            entry.grid(row=row, column=1, padx=10)
            entry.insert(0, str(value))
            self.entries.append(entry)
        ttk.Label(frame, text='The estimate uses the S9 reference in Setup.').grid(
            row=4, columnspan=2, sticky='w', padx=10, pady=4)
        return self.entries[0]

    def validate(self):
        if not self.enabled.get():
            self.result = (None,)
            return True
        try:
            threshold, duration = (int(entry.get()) for entry in self.entries)
            if -200 <= threshold <= 50 and 1 <= duration <= 3600:
                self.result = ([threshold, duration],)
                return True
        except ValueError:
            pass
        messagebox.showinfo(self.title(), 'Enter a threshold from −200 to 50 dBm and a duration from 1 to 3600 seconds.',
                            parent=self)
        return False


class Plotter(tk.Tk):
    def __init__(self):
        super().__init__()
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
        self.plot_comments = {}
        self.plot_view = None
        self.comment_hitboxes = []
        self.measurement = []
        self.measuring = False
        self.measurement_candidates = []
        self.comparison = None
        self.focus_frequency = None
        self.scan_beacons = []
        self.scan_index = 0
        self.scan_job = None
        self.scan_cancel = None
        self.recording = DailyRecording()
        self.recording_error = ''
        self.lift_detector = LiftDetector()
        self.alert_status = None
        self.plot_cleared_at = float('-inf')
        self.time_offset = time.time() - time.monotonic()
        self.loaded_plot = False
        self.last_draw = None
        self.last_draw_at = float('-inf')
        self.footer_contents = None
        self.view_end = None
        self.stop = None
        self.radio_responding = False
        self.radio_verified = False
        self.pause = Event()
        self.pause_time = None
        self.settings_path = settings_file()
        first_run = not self.settings_path.exists()
        settings = load_settings(self.settings_path)
        self.lift_alerts = alert_preferences(settings.get('lift_alerts'))
        folder = settings.get('recording_folder')
        self.recording_folder = Path(folder) if isinstance(folder, str) and folder.strip() else None
        scan_frequencies = settings.get('scan_frequencies', [])
        self.scan_frequencies = ([hz for hz in scan_frequencies if type(hz) is int]
                                 if isinstance(scan_frequencies, list) else [])
        dwell = settings.get('scan_dwell')
        self.scan_dwell = dwell if type(dwell) is int and 5 <= dwell <= 3600 else 15
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
        self.auto_connect_job = None
        self.current_hz = None
        self.tuned_beacon = None
        model, address = radio_setup(settings)
        self.selected_radio = model
        saved_bauds = settings.get('radio_bauds', {})
        self.radio_bauds = saved_bauds if isinstance(saved_bauds, dict) else {}
        default_baud = '38400' if model in YAESU_MODELS else '115200'
        self.baud = tk.StringVar(value=settings.get('baud', default_baud))
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
        self.show_signal_guide = settings.get('show_signal_guide') is not False
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
        self.title(f'{self.plotter_heading} v{APP_VERSION}')
        self.station_locator = locator.strip().upper() if isinstance(locator, str) else ''
        self.station_coordinates = None
        key = settings.get('google_maps_key')
        self.google_maps_key = key if isinstance(key, str) else ''
        self.map_server = None
        self.synoptic_window = None
        self.synoptic_auto_bom = settings.get('synoptic_auto_bom') is True
        self.bom_chart_url = ''
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
        self.status = tk.StringVar(value='Select the radio and its COM port.')
        self.readout = tk.StringVar(value=f'{self.raw_meter_label}  —')
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
                                        values=RADIO_MODELS, width=16, state='readonly')
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
        self.update_address_field()
        self.inputs[0].configure(postcommand=self.refresh_ports)
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
        menu.add_command(label='Compare saved plot…', command=self.open_comparison)
        menu.add_command(label='Remove comparison', command=self.remove_comparison)
        menu.add_separator()
        menu.add_command(label='Export CSV…', command=self.export_csv)
        files.configure(menu=menu)
        map_button = ttk.Menubutton(readouts, text='Map')
        map_button.pack(side='left', padx=(8, 0))
        map_menu = tk.Menu(map_button, tearoff=False)
        map_menu.add_command(label='Google Maps and elevation', command=self.open_beacon_map)
        map_menu.add_command(label='OK1CDJ PathProfile', command=self.open_path_profile)
        map_button.configure(menu=map_menu)
        ttk.Button(readouts, text='Synoptic', command=self.open_synoptic_chart).pack(side='left', padx=(8, 0))
        ttk.Button(readouts, text='Hepburn Chart', command=self.open_hepburn_chart).pack(
            side='left', padx=(8, 0))
        help_button = ttk.Menubutton(readouts, text='Help')
        help_button.pack(side='left', padx=(8, 0))
        help_menu = tk.Menu(help_button, tearoff=False)
        help_menu.add_command(label='Quick Start',
                              command=lambda: self.open_guide('Beacon-Plotter-Quick-Start.pdf'))
        help_menu.add_command(label='Operating Guide',
                              command=lambda: self.open_guide('Beacon-Plotter-Operating-Guide.pdf'))
        help_button.configure(menu=help_menu)
        tools_button = ttk.Menubutton(readouts, text='Tools')
        tools_button.pack(side='left', padx=(8, 0))
        tools_menu = tk.Menu(tools_button, tearoff=False)
        for label, command in (
            ('Measure two points', self.start_measurement), ('Clear measurement', self.clear_measurement),
            ('Beacon lift alert…', self.configure_lift_alert), ('Start daily recording…', self.start_recording),
            ('Stop daily recording', self.stop_recording), ('Scan beacons…', self.start_scan),
            ('Stop scanning', self.stop_scan), ('Filter plot by frequency…', self.choose_focus_frequency),
        ):
            tools_menu.add_command(label=label, command=command)
        tools_button.configure(menu=tools_menu)
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
        self.signal_level_font = font.Font(root=self, family='Segoe UI', size=9, weight='bold')
        self.plot_comment_menu = tk.Menu(self.chart, tearoff=False)
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
        self.chart.bind('<Button-3>', self.open_plot_comment_menu)
        self.chart.bind('<Button-1>', self.place_measurement_cursor)
        self.history_scroll = ttk.Scrollbar(self, orient='horizontal', command=self.scroll_history)
        self.history_scroll.grid(row=5, column=0, columnspan=2, sticky='ew', padx=22)
        self.chart.bind('<Configure>', lambda event: self.draw())
        self.chart.bind('<Map>', lambda event: self.after_idle(self.draw))
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
        self.after(15_000, self.recording_tick)
        self.after(1000, self.update_display_clock)
        if first_run:
            self.after_idle(self.prompt_first_run_station)
        elif self.auto_connect and self.last_connected_radio:
            self.after_idle(self.start_radio_auto_connect)

    def prompt_first_run_station(self):
        result = FirstRunStationDialog(self, 'Your station').result
        if result:
            self.station_callsign, self.station_locator, self.station_coordinates = result
            self.update_station_title()
            self.sync_beacon()
        self.persist_settings()

    @property
    def plotter_heading(self):
        return f'{self.station_callsign} {PLOTTER_HEADING}' if self.station_callsign else PLOTTER_HEADING

    def update_station_title(self):
        self.title(f'{self.plotter_heading} v{APP_VERSION}')
        self.update_backdrop()

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

    def open_guide(self, filename):
        app_file = sys.executable if getattr(sys, 'frozen', False) else __file__
        path = Path(app_file).resolve().parent / 'docs' / filename
        if not path.is_file():
            messagebox.showwarning('Help', 'This guide is missing from the app’s docs folder.', parent=self)
            return
        try:
            if os.name == 'nt':
                os.startfile(str(path))
            elif not webbrowser.open(path.as_uri()):
                raise OSError('No PDF viewer could be opened.')
        except OSError as exc:
            messagebox.showwarning('Help', f'Could not open the guide.\n\n{exc}', parent=self)

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
        if self.tuned_beacon is not None and hz != self.tuned_beacon[0]:
            self.tuned_beacon = None
        self.frequency.set(format_radio_frequency(hz))
        self.sync_beacon()

    def callsign_for_frequency(self, hz):
        if hz is None:
            return None
        if self.tuned_beacon is not None and hz == self.tuned_beacon[0]:
            return self.tuned_beacon[1]
        matches = {beacon['callsign'] for beacon in self.beacons if beacon['hz'] == hz}
        return matches.pop() if len(matches) == 1 else None

    def beacon_map_details(self):
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
        return (station_name, beacon['callsign'], self.station_coordinates, destination,
                format_beacon_path(self.station_coordinates, locator))

    def open_path_profile(self):
        details = self.beacon_map_details()
        if details is None:
            return
        _, _, origin, destination, _ = details
        try:
            if not webbrowser.open(path_profile_url(origin, destination)):
                raise OSError('The default browser could not be opened.')
        except OSError as exc:
            messagebox.showwarning('Beacon map', str(exc), parent=self)

    def open_beacon_map(self):
        details = self.beacon_map_details()
        if details is None:
            return
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

        auto_bom = self.synoptic_auto_bom
        source = self.bom_chart_url if auto_bom else self.synoptic_source
        if not auto_bom and not source:
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
            url = source
            stale = False
            if auto_bom:
                try:
                    url = latest_bom_chart_url()
                except (OSError, ValueError):
                    stale = True
            try:
                image = read_synoptic_chart(url, screen_size) if url else None
            except (OSError, ValueError, Image.DecompressionBombError):
                image = None
            result.put((url, image, stale))

        def show():
            if not window.winfo_exists():
                return
            try:
                url, image, stale = result.get_nowait()
            except Empty:
                self.after(100, show)
                return
            if image is None:
                chart.configure(text='Could not load the latest BoM chart. Try again shortly.' if auto_bom else
                                'Could not load the chart. Check the image URL in Setup.')
            else:
                if auto_bom:
                    self.bom_chart_url = url
                if stale:
                    window.title('Synoptic — last fetched BoM chart')
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
        self.stop_scan()
        self.queue_beacon_tune(beacon)

    def queue_beacon_tune(self, beacon, *, scanning=False):
        if self.stop is None or self.stop.is_set() or self.auto_connecting or self.tuning:
            return False
        request = ('scan_beacon', (self.scan_cancel, beacon)) if scanning else ('beacon', beacon)
        try:
            self.radio_requests.put_nowait(request)
        except Full:
            return False
        self.tuning = True
        self.beacon_picker.configure(state='disabled')
        self.status.set(f"Tuning to {beacon['callsign']}…")
        return True

    def start_scan(self):
        if self.stop is None or self.stop.is_set() or not self.radio_verified or self.pause.is_set() or self.tuning:
            messagebox.showinfo('Scan beacons', 'Connect the radio and resume sampling before scanning.', parent=self)
            return
        beacons = {}
        for beacon in self.beacons:
            if supports_frequency(self.model.get(), beacon['hz']):
                beacons.setdefault(beacon['hz'], beacon)
        if len(beacons) < 2:
            messagebox.showinfo('Scan beacons', 'Save at least two frequencies supported by this radio.', parent=self)
            return
        result = ScanDialog(self, beacons, self.scan_frequencies, self.scan_dwell).result
        if result is None or self.stop is None or self.stop.is_set():
            return
        self.stop_scan()
        self.scan_beacons, self.scan_dwell = result
        self.scan_frequencies = [beacon['hz'] for beacon in self.scan_beacons]
        self.scan_cancel = Event()
        self.scan_index = 0
        self.persist_settings()
        if not self.tuning:
            self.scan_step()

    def scan_step(self):
        self.scan_job = None
        if not self.scan_beacons or self.pause.is_set():
            return
        beacon = self.scan_beacons[self.scan_index]
        if not self.queue_beacon_tune(beacon, scanning=True):
            self.stop_scan()
            return
        self.scan_index = (self.scan_index + 1) % len(self.scan_beacons)

    def schedule_scan_step(self):
        if self.scan_beacons and not self.pause.is_set() and self.scan_job is None:
            self.scan_job = self.after(self.scan_dwell * 1000, self.scan_step)

    def stop_scan(self):
        if self.scan_job is not None:
            self.after_cancel(self.scan_job)
            self.scan_job = None
        if self.scan_cancel is not None:
            self.scan_cancel.set()
            self.scan_cancel = None
        self.scan_beacons = []
        if self.active_theme is not None:
            self.update_footer()

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
        previous = self.selected_radio
        self.radio_bauds[previous] = self.baud.get()
        self.selected_radio = model
        self.port.set(self.radio_ports.get(model, ''))
        if model in YAESU_MODELS:
            default_baud = '38400'
        else:
            default_baud = self.radio_bauds.get(previous, '115200') if previous in RADIO_ADDRESSES else '115200'
        self.baud.set(self.radio_bauds.get(model, default_baud))
        self.update_address_field()
        address = RADIO_ADDRESSES.get(model)
        if address:
            self.address.set(address)
        if not self.history:
            self.plot_model = model
            self.readout.set(f'{self.raw_meter_label}  —')
            self.draw()

    @property
    def raw_meter_label(self):
        return 'Raw CAT' if self.plot_model in YAESU_MODELS else 'Raw CI-V'

    def update_address_field(self):
        if self.model.get() in YAESU_MODELS:
            self.inputs[2].grid_remove()
        else:
            self.inputs[2].grid()
        if hasattr(self, 'setup_panels') and hasattr(self, 'chart'):
            self.after_idle(self.update_backdrop)

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

    def start_measurement(self):
        if not self.history:
            messagebox.showinfo('Measure two points', 'Record or open a plot first.', parent=self)
            return
        self.measurement.clear()
        self.measuring = True
        self.chart.configure(cursor='crosshair')
        self.draw()

    def clear_measurement(self, *, redraw=True):
        self.measuring = False
        self.measurement.clear()
        self.chart.configure(cursor='')
        if redraw:
            self.draw()

    def place_measurement_cursor(self, event):
        if not self.measuring or not self.measurement_candidates:
            return
        left, top, right, bottom, start, end = self.plot_view
        if not (left <= event.x <= right and top <= event.y <= bottom):
            return
        stamp = start + (event.x - left) / (right - left) * (end - start)
        tolerance = (end - start) / (right - left)
        if not self.measurement_candidates[0][0] - tolerance <= stamp <= self.measurement_candidates[-1][0] + tolerance:
            return
        sample = min(self.measurement_candidates, key=lambda reading: abs(reading[0] - stamp))
        if self.measurement and self.measurement[0][0] == sample[0]:
            return
        self.measurement.append(sample)
        if len(self.measurement) == 2:
            self.measuring = False
            self.chart.configure(cursor='')
        self.draw()
        return 'break'

    def measurement_status(self):
        if self.measuring:
            return 'Click point B on the trace.' if self.measurement else 'Click point A on the trace.'
        if len(self.measurement) != 2:
            return None
        first, second = self.measurement
        elapsed = abs(second[0] - first[0])
        if first[2] != second[2] and first[2] is not None and second[2] is not None:
            return f'A → B: {elapsed:.1f} s · different frequencies'
        if self.plot_model in YAESU_MODELS or (self.axis_units.get() != 'dBm' and self.plot_model == 'Custom Icom'):
            delta = second[1] - first[1]
            return f'A → B: {elapsed:.1f} s · {delta:+d} {self.raw_meter_label.lower()} counts'
        positions = IC9700_POSITIONS if self.plot_model == 'IC-9700' else STANDARD_POSITIONS
        delta = positions[second[1]] - positions[first[1]]
        return f'A → B: {elapsed:.1f} s · {delta:+.1f} dB'

    def configure_lift_alert(self):
        if self.model.get() in YAESU_MODELS:
            messagebox.showinfo('Beacon lift alert', 'dBm thresholds require a calibrated meter scale.', parent=self)
            return
        index = self.beacon_picker.current()
        if not 0 <= index < len(self.beacons):
            messagebox.showinfo('Beacon lift alert', 'Select a saved beacon frequency first.', parent=self)
            return
        beacon = self.beacons[index]
        key = self.lift_key(beacon['hz'], beacon['callsign'])
        result = LiftAlertDialog(self, beacon, self.lift_alerts.get(key)).result
        if result is not None:
            rule, = result
            if rule is None:
                self.lift_alerts.pop(key, None)
            else:
                self.lift_alerts[key] = rule
            self.lift_detector.clear(key)
            self.alert_status = None
            self.persist_settings()
            self.update_footer()

    @staticmethod
    def lift_key(hz, callsign):
        return f'{beacon_callsign_key(callsign)}:{hz}' if callsign and hz is not None else None

    def check_lift(self, sample):
        if self.plot_model in YAESU_MODELS:
            return
        stamp, raw, hz, callsign = sample
        key = self.lift_key(hz, callsign)
        rule = self.lift_alerts.get(key)
        level = None
        if raw is not None and rule is not None:
            positions = IC9700_POSITIONS if self.plot_model == 'IC-9700' else STANDARD_POSITIONS
            level = DBM_REFERENCES[self.dbm_reference.get()] + positions[raw] - 54
        if self.lift_detector.update(key, stamp, level, rule, max_gap=2 * self.sample_interval + 1):
            self.alert_status = (time.monotonic() + 10, f'Lift alert · {callsign} · {level:.0f} dBm')
            if os.name == 'nt':
                import winsound
                try:
                    winsound.MessageBeep()
                except RuntimeError:
                    self.bell()
            else:
                self.bell()
            self.update_footer()

    def start_recording(self):
        directory = filedialog.askdirectory(parent=self, title='Choose the daily recording folder',
                                            initialdir=self.recording_folder or Path.home())
        if not directory:
            return
        folder = Path(directory)
        if not self.flush_recording():
            self.recording.redirect_pending(folder)
            self.flush_recording()
        self.recording_folder = folder
        self.persist_settings()
        self.update_footer()

    def stop_recording(self):
        self.recording_folder = None
        self.flush_recording()
        self.persist_settings()

    def flush_recording(self):
        errors = self.recording.flush(utc_day(time.time()), PLOT_FORMAT)
        self.recording_error = errors[0] if errors else ''
        self.update_footer()
        return not errors

    def recording_tick(self):
        self.flush_recording()
        self.after(30_000, self.recording_tick)

    def record_sample(self, sample):
        if self.recording_folder is not None:
            stamp, raw, hz, callsign = sample
            self.recording.append(self.recording_folder, self.plot_model,
                                  (stamp + self.time_offset, raw, hz, callsign))

    def record_comment(self, stamp, text):
        if self.recording_folder is not None and not self.loaded_plot:
            self.recording.comment(self.recording_folder, self.plot_model, stamp + self.time_offset, text)

    def open_plot_comment_menu(self, event):
        if not self.history or self.plot_view is None:
            return
        left, top, right, bottom, start, end = self.plot_view
        if not (left <= event.x <= right and top <= event.y <= bottom):
            return
        stamp = start + (event.x - left) / (right - left) * (end - start)
        for existing, column, bounds in reversed(self.comment_hitboxes):
            if bounds is not None:
                x0, y0, x1, y1 = bounds
                if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                    stamp = existing
                    break
            if abs(event.x - column) <= 4:
                stamp = existing
                break
        menu = self.plot_comment_menu
        menu.delete(0, 'end')
        if stamp in self.plot_comments:
            menu.add_command(label='Edit comment…', command=lambda: self.edit_plot_comment(stamp))
            menu.add_command(label='Delete comment', command=lambda: self.delete_plot_comment(stamp))
        else:
            # A pixel can represent several seconds at wide zoom levels.
            tolerance = (end - start) / (right - left) / 2
            oldest, newest = self.history[0][0], self.history[-1][0]
            if not oldest - tolerance <= stamp <= newest + tolerance:
                return
            stamp = min(newest, max(oldest, stamp))
            menu.add_command(label='Add comment…', command=lambda: self.edit_plot_comment(stamp))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def edit_plot_comment(self, stamp):
        clock = datetime.fromtimestamp(stamp + self.time_offset).strftime('%H:%M:%S')
        initial = self.plot_comments.get(stamp, '')
        while True:
            text = simpledialog.askstring(
                'Plot comment', f'Comment at {clock} local time (up to {PLOT_COMMENT_LIMIT} characters)',
                initialvalue=initial, parent=self,
            )
            if text is None or not text.strip():
                return
            initial = ' '.join(text.split())
            if len(initial) <= PLOT_COMMENT_LIMIT and initial.isprintable():
                self.plot_comments[stamp] = initial
                self.record_comment(stamp, initial)
                self.draw()
                return
            messagebox.showwarning('Plot comment', 'Enter a shorter comment using printable text.', parent=self)

    def delete_plot_comment(self, stamp):
        self.plot_comments.pop(stamp, None)
        self.record_comment(stamp, None)
        self.draw()

    def clear_plot(self):
        if not self.history or not messagebox.askyesno(
            'Clear Plot', 'Are you sure you want to clear the current plot?', parent=self, default=messagebox.NO,
        ):
            return
        self.plot_cleared_at = time.monotonic()
        self.history.clear()
        self.plot_comments.clear()
        self.clear_measurement(redraw=False)
        self.comparison = None
        self.focus_frequency = None
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
            if not self.tuning:
                self.schedule_scan_step()
        else:
            self.pause.set()
            self.pause_time = time.monotonic()
            self.pause_button.configure(text='Resume')
            self.status.set('Paused · captured readings are retained.')
            self.lift_detector.reset()
            if self.scan_job is not None:
                self.after_cancel(self.scan_job)
                self.scan_job = None
        self.draw()

    def latest_time(self):
        if self.comparison is not None:
            return max(self.history[-1][0], self.comparison[1][-1][0])
        if self.pause_time is not None:
            return self.pause_time
        if self.stop is None and self.history:
            return self.history[-1][0]
        return time.monotonic()

    def oldest_plot_time(self, latest):
        if self.comparison is not None:
            return min(self.history[0][0], self.comparison[1][0][0])
        return self.history[0][0] if self.history else latest

    def scroll_history(self, action, value, unit=None):
        if not self.history:
            return
        latest = self.latest_time()
        span = TIME_WINDOWS[self.window_index.get()][0]
        oldest = self.oldest_plot_time(latest)
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
        try:
            ports = available_serial_ports()
        except OSError as exc:
            self.status.set(f'Could not list COM ports: {exc}')
            return
        self.inputs[0]['values'] = ports
        if not self.port.get() and len(ports) == 1:
            self.port.set(ports[0])

    def start_radio_auto_connect(self, *, preserve_history=False):
        if not self.auto_connect or self.stop is not None:
            return
        saved = self.last_connected_radio
        if not saved:
            return
        self.model.set(saved['model'])
        self.selected_radio = saved['model']
        self.update_address_field()
        self.port.set(saved['port'])
        self.baud.set(saved['baud'])
        if saved['model'] in RADIO_ADDRESSES:
            self.address.set(saved['address'])
        self.retry_radio_auto_connect(time.monotonic() + AUTO_CONNECT_WINDOW, preserve_history)

    def retry_radio_auto_connect(self, deadline, preserve_history):
        self.auto_connect_job = None
        if not self.auto_connect:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if self.auto_connecting and self.stop is not None:
                self.stop.set()
            self.status.set(f'Auto-connect timed out on {self.port.get()}. Click Connect to try again.')
            return
        saved = self.last_connected_radio
        address = self.address.get() if self.model.get() in RADIO_ADDRESSES else ''
        if (self.model.get(), self.port.get(), self.baud.get(), address) != (
            saved['model'], saved['port'], str(saved['baud']), saved['address'],
        ):
            self.status.set('Radio settings changed. Click Connect to use the new settings.')
            return
        delay = min(AUTO_CONNECT_INTERVAL * 1000, ceil(remaining * 1000))
        self.auto_connect_job = self.after(delay, self.retry_radio_auto_connect, deadline, preserve_history)
        if self.stop is None:
            self.toggle_connection(automatic=True, preserve_history=preserve_history)

    def cancel_radio_auto_connect(self):
        if self.auto_connect_job is not None:
            self.after_cancel(self.auto_connect_job)
            self.auto_connect_job = None

    def toggle_connection(self, automatic=False, *, preserve_history=False):
        if not automatic:
            self.cancel_radio_auto_connect()
        if self.stop is not None:
            self.stop_scan()
            self.stop.set()
            self.set_radio_responding(False)
            self.connect.configure(state='disabled')
            self.pause_button.configure(state='disabled')
            self.mute_button.configure(state='disabled')
            self.status.set('Disconnecting…')
            return
        yaesu = self.model.get() in YAESU_MODELS
        try:
            port = self.port.get().strip()
            baud = int(self.baud.get())
            address = None if yaesu else int(self.address.get().strip(), 16)
            if not port or baud <= 0 or (address is not None and not 1 <= address < 0xE0):
                raise ValueError
        except ValueError:
            fields = 'the Enhanced COM port and CAT baud rate' if yaesu else 'a COM port, baud rate and hex CI-V address'
            messagebox.showwarning('Connection settings', f'Enter {fields}.')
            return
        if self.loaded_plot and not messagebox.askokcancel(
            'Start live plot', 'Connecting will replace the displayed plot. Continue?', parent=self,
        ):
            return
        if not preserve_history:
            self.history.clear()
            self.plot_comments.clear()
            self.clear_measurement(redraw=False)
            self.comparison = None
            self.focus_frequency = None
            self.time_offset = time.time() - time.monotonic()
            self.view_end = None
        self.loaded_plot = False
        self.plot_model = self.model.get()
        self.draw()
        self.readout.set(f'{self.raw_meter_label}  —')
        self.stop = Event()
        self.radio_verified = False
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
                  lambda: self.sample_interval, self.plot_model),
            daemon=True,
        ).start()

    def toggle_radio_mute(self):
        if self.stop is None or self.stop.is_set() or self.audio_pending or self.plot_model in YAESU_MODELS:
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
                    value = payload[1]
                    if self.stop.is_set():
                        continue
                    if self.auto_connecting:
                        if value is None:
                            self.stop.set()
                            continue
                        self.auto_connecting = False
                        self.cancel_radio_auto_connect()
                        self.pause_button.configure(state='normal')
                    if value is not None:
                        self.radio_verified = True
                    if payload[0] >= self.plot_cleared_at:
                        sample = (*payload, self.callsign_for_frequency(payload[2]))
                        self.history.append(sample)
                        self.record_sample(sample)
                        if not self.pause.is_set():
                            self.check_lift(sample)
                    self.set_radio_responding(value is not None and self.stop is not None and not self.stop.is_set())
                    self.readout.set(f"{self.raw_meter_label}  {'—' if value is None else value}")
                    if not self.tuning and not self.pause.is_set():
                        connection = ('Enhanced COM port and CAT baud rate' if self.plot_model in YAESU_MODELS
                                      else 'COM port, baud and address')
                        self.status.set(f'No meter reply. Check the radio’s {connection}.'
                                        if value is None else f'Receiving meter readings every {self.sample_interval:g} s.')
                    if (value is not None and not self.audio_pending and not self.stop.is_set()
                            and self.plot_model in RADIO_ADDRESSES):
                        self.mute_button.configure(state='normal')
                    self.remember_port(value)
                elif kind == 'frequency':
                    self.show_frequency(payload)
                elif kind == 'plot_gap':
                    self.lift_detector.reset()
                    if payload >= self.plot_cleared_at:
                        gap = (payload, None, None, None)
                        self.history.append(gap)
                        self.record_sample(gap)
                elif kind in ('tuned', 'tune_failed', 'tune_cancelled'):
                    self.tuning = False
                    self.beacon_picker.configure(state='readonly')
                    if kind == 'tuned':
                        self.tuned_beacon = payload['hz'], payload['callsign']
                        self.show_frequency(payload['hz'])
                        suffix = ' · paused' if self.pause.is_set() else ''
                        mode = 'CW' if self.plot_model in YAESU_MODELS else 'CW / Filter 1'
                        self.status.set(f"Tuned to {payload['callsign']} · {mode}{suffix}")
                        self.schedule_scan_step()
                    elif kind == 'tune_failed':
                        self.stop_scan()
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
                    if self.stop.is_set():
                        continue
                    self.set_radio_responding(False)
                    self.status.set(f"Could not auto-connect on {self.port.get()}. {payload}"
                                    if self.auto_connecting else payload)
                    self.readout.set(f'{self.raw_meter_label}  —')
                elif kind == 'closed':
                    recover = payload and self.auto_connect and self.radio_verified and not self.stop.is_set()
                    self.stop_scan()
                    self.lift_detector.clear()
                    self.alert_status = None
                    if self.history:
                        gap = (time.monotonic(), None, None, None)
                        self.record_sample(gap)
                        if recover:
                            self.history.append(gap)
                    self.flush_recording()
                    self.set_radio_responding(False)
                    if self.auto_connecting and self.auto_connect_job is not None:
                        self.status.set(f'Waiting for the radio on {self.port.get()}. Retrying automatically…')
                    elif self.status.get() == 'Disconnecting…':
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
                    self.readout.set(f'{self.raw_meter_label}  —')
                    self.draw()
                    self.connect.configure(text='Connect', state='normal')
                    self.radio_picker.configure(state='readonly')
                    for widget in (*self.inputs, self.refresh):
                        widget.configure(state='normal')
                    if recover:
                        self.start_radio_auto_connect(preserve_history=True)
        except Empty:
            pass
        if self.history and self.last_draw != self.history[-1]:
            if self.state() == 'iconic' or not self.chart.winfo_viewable():
                self.prune_plot_history(self.latest_time())
            elif (TIME_WINDOWS[self.window_index.get()][0] != HISTORY_SECONDS
                  or time.monotonic() - self.last_draw_at >= LONG_VIEW_REDRAW_INTERVAL):
                self.draw()
        self.after(250, self.poll)

    def update_footer(self, *_args):
        if self.active_theme is None:
            return
        width, height = self.footer.winfo_width(), self.footer.winfo_height()
        colour = DISPLAY_THEMES[self.active_theme]['text']
        status = self.measurement_status() or self.status.get()
        if self.alert_status and time.monotonic() < self.alert_status[0]:
            status = self.alert_status[1]
        if self.recording_error:
            status = 'Daily recording failed · ' + self.recording_error
        elif self.recording_folder is not None:
            status += ' · Daily recording armed' if self.stop is None else ' · Daily recording'
        if self.scan_beacons:
            status += ' · Scanning'
        readout = self.readout.get()
        contents = width, height, colour, status, readout
        if contents == self.footer_contents:
            return
        self.footer_contents = contents
        self.footer.delete('readouts')
        self.footer.create_text(12, height / 2, text=status, anchor='w',
                                fill=colour, tags='readouts')
        self.footer.create_text(width - 12, height / 2, text=readout, anchor='e',
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
        return [(timestamp + self.time_offset, raw, hz, callsign)
                for timestamp, raw, hz, callsign in self.history]

    def save_plot(self):
        path = self.choose_plot_output('.bplot', 'Beacon plot')
        if path is None:
            return
        try:
            save_settings(path, {'format': PLOT_FORMAT, 'radio_model': self.plot_model,
                                 'samples': self.timestamped_samples(),
                                 'comments': [(stamp + self.time_offset, text)
                                              for stamp, text in sorted(self.plot_comments.items())]})
        except OSError as exc:
            messagebox.showwarning('Save plot', str(exc), parent=self)

    def open_plot(self):
        if self.stop is not None:
            messagebox.showinfo('Open saved plot', 'Disconnect the radio before opening a saved plot.', parent=self)
            return
        self.cancel_radio_auto_connect()
        path = filedialog.askopenfilename(parent=self, filetypes=[('Beacon plot', '*.bplot')])
        if not path:
            return
        try:
            model, history, comments = read_plot(Path(path))
        except (OSError, ValueError, RecursionError) as exc:
            messagebox.showwarning('Open saved plot', str(exc), parent=self)
            return
        if self.history and not messagebox.askokcancel(
            'Open saved plot', 'Replace the currently displayed plot?', parent=self,
        ):
            return
        self.history = history
        self.plot_comments = comments
        self.clear_measurement(redraw=False)
        self.comparison = None
        self.focus_frequency = None
        self.loaded_plot = True
        self.time_offset = 0
        self.plot_model = model
        self.readout.set(f'{self.raw_meter_label}  —')
        self.view_end = None
        duration = history[-1][0] - history[0][0]
        index = next((i for i, (span, _, _) in enumerate(TIME_WINDOWS) if span >= duration),
                     len(TIME_WINDOWS) - 1)
        self.window_index.set(index)
        self.status.set(f'Saved plot · {Path(path).name}')
        self.change_window(index)

    @staticmethod
    def recording_frequencies(samples):
        return {hz for _, raw, hz, _ in samples if raw is not None and hz is not None}

    @staticmethod
    def frequency_choices(frequencies):
        return {f'{hz / 1_000_000:.6f}'.rstrip('0').rstrip('.') + ' MHz': hz
                for hz in sorted(frequencies)}

    def choose_focus_frequency(self):
        if self.comparison is not None:
            messagebox.showinfo('Filter plot', 'Remove the comparison to change the plot frequency filter.',
                                parent=self)
            return
        choices = {'All frequencies': None, **self.frequency_choices(
            self.recording_frequencies(self.history) | {beacon['hz'] for beacon in self.beacons})}
        dialog = FrequencyDialog(self, choices, 'Filter plot by frequency')
        if dialog.result is not None:
            self.focus_frequency, = dialog.result
            self.clear_measurement(redraw=False)
            self.draw()

    def open_comparison(self):
        if not self.loaded_plot:
            messagebox.showinfo('Compare saved plots', 'Open the first saved plot before choosing a comparison.',
                                parent=self)
            return
        path = filedialog.askopenfilename(parent=self, title='Choose a comparison plot',
                                          filetypes=[('Beacon plot', '*.bplot')])
        if not path:
            return
        try:
            model, samples, _ = read_plot(Path(path))
        except (OSError, ValueError, RecursionError) as exc:
            messagebox.showwarning('Compare saved plots', str(exc), parent=self)
            return
        if model != self.plot_model and (model in YAESU_MODELS or self.plot_model in YAESU_MODELS):
            messagebox.showinfo('Compare saved plots', 'Compare raw CAT readings from the same radio model.',
                                parent=self)
            return
        matching = self.recording_frequencies(self.history) & self.recording_frequencies(samples)
        if not matching:
            messagebox.showinfo('Compare saved plots', 'The two plots have no matching recorded frequency.',
                                parent=self)
            return
        if len(matching) == 1:
            hz, = matching
        else:
            result = FrequencyDialog(self, self.frequency_choices(matching), 'Choose the comparison frequency').result
            if result is None:
                return
            hz, = result
        aligned = align_recording(self.history[0][0], samples)
        self.comparison = (model, aligned, hz, Path(path).name)
        self.clear_measurement(redraw=False)
        self.status.set(f'Comparison · {Path(path).name} · aligned by UTC time of day')
        latest = self.latest_time()
        duration = latest - self.oldest_plot_time(latest)
        index = next((i for i, (span, _, _) in enumerate(TIME_WINDOWS) if span >= duration),
                     len(TIME_WINDOWS) - 1)
        self.view_end = None
        self.window_index.set(index)
        self.change_window(index)

    def remove_comparison(self):
        if self.comparison is not None:
            self.comparison = None
            self.clear_measurement(redraw=False)
            self.status.set('Comparison removed.')
            self.draw()

    def export_csv(self):
        path = self.choose_plot_output('.csv', 'CSV files')
        if path is None:
            return
        try:
            write_plot_csv(path, self.timestamped_samples(), self.plot_model)
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
            previous_callsign = self.station_callsign
            (self.plot_transparency, self.warn_on_close, self.auto_connect,
             units, reference, mode, smoothing, self.station_callsign, self.station_locator,
             self.station_coordinates, self.sample_interval, synoptic_source, synoptic_auto_bom,
             bom_chart_url, self.hepburn_source, self.show_signal_guide) = result
            if not self.auto_connect and self.auto_connect_job is not None:
                self.cancel_radio_auto_connect()
                if self.auto_connecting and self.stop is not None:
                    self.toggle_connection()
                else:
                    self.status.set('Radio auto-connect is off. Click Connect to start manually.')
            if (synoptic_source, synoptic_auto_bom, bom_chart_url) != (
                self.synoptic_source, self.synoptic_auto_bom, self.bom_chart_url,
            ):
                self.synoptic_source = synoptic_source
                self.synoptic_auto_bom = synoptic_auto_bom
                self.bom_chart_url = bom_chart_url
                if self.synoptic_window is not None and self.synoptic_window.winfo_exists():
                    self.synoptic_window.destroy()
            self.axis_units.set(units)
            if self.dbm_reference.get() != reference:
                self.lift_detector.clear()
            self.dbm_reference.set(reference)
            self.display_theme.set(mode)
            self.smoothing.set(smoothing)
            self.sync_beacon()
            self.persist_settings()
        self.apply_display_theme()
        if result is not None and self.station_callsign != previous_callsign:
            self.update_station_title()
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
        heading = self.plotter_heading
        key = (theme, width, height, bounds, heading, self.model.get() in YAESU_MODELS)
        if key == self.backdrop_key or min(width, height) < 2:
            return
        self.backdrop_key = key
        palette = DISPLAY_THEMES[theme]
        heading_drawn = False
        scene = None
        heading_colour = '#103047' if theme == 'Day' else '#f1f7ff'
        # Leave room for the right-aligned local and UTC clocks.
        heading_width = max(1, width - 180)
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

            asset = Path(__file__).resolve().with_name(f'beacon_background_{theme.lower()}.png')
            with Image.open(asset) as source:
                scene = ImageOps.fit(source.convert('RGB'), (width, height), Image.Resampling.LANCZOS)
            try:
                font_file = asset.with_name('OpenSans-Bold.ttf')
                heading_font = ImageFont.truetype(str(font_file), size=44)
                measured_width = heading_font.getlength(heading)
                if measured_width > heading_width:
                    heading_font = heading_font.font_variant(size=max(1, int(44 * heading_width / measured_width)))
            except OSError:
                pass
            else:
                draw = ImageDraw.Draw(scene)
                _, top, _, bottom = draw.textbbox((0, 0), heading, font=heading_font)
                draw.text((24, 45 - (top + bottom) / 2), heading, font=heading_font,
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
            heading_font = font.Font(root=self, family='Segoe UI', size=25, weight='bold')
            measured_width = heading_font.measure(heading)
            if measured_width > heading_width:
                heading_font.configure(size=max(1, int(25 * heading_width / measured_width)))
            self.backdrop.create_text(24, 45, text=heading, anchor='w',
                                      font=heading_font, fill=heading_colour)
            self.backdrop.heading_font = heading_font

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
                if not widget.winfo_manager():
                    continue
                panel.create_text(widget.winfo_x(), 29, text=label, anchor='w', fill=palette['text'])

        self.footer_contents = None
        self.update_footer()
        self.draw_clocks()
        self.render_plot_wallpaper()
        self.draw()

    def prune_plot_history(self, latest):
        if self.loaded_plot:
            return
        cutoff = latest - HISTORY_SECONDS
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()
        if self.plot_comments:
            self.plot_comments = {stamp: text for stamp, text in self.plot_comments.items() if stamp >= cutoff}

    def draw(self):
        theme = self.active_theme
        palette = DISPLAY_THEMES[theme]
        trace_colours = SIGNAL_COLOURS[theme]
        canvas = self.chart
        canvas.delete('all')
        if canvas.wallpaper_photo is not None:
            canvas.create_image(0, 0, image=canvas.wallpaper_photo, anchor='nw', tags='wallpaper')
        latest = self.latest_time()
        self.prune_plot_history(latest)
        self.clear_plot_button.configure(state='normal' if self.history else 'disabled')
        self.last_draw = self.history[-1] if self.history else None
        self.last_draw_at = time.monotonic()
        span, _, interval = TIME_WINDOWS[self.window_index.get()]
        oldest = self.oldest_plot_time(latest)
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
        zoom_left = canvas.winfo_width() - self.y_zoom_controls.winfo_reqwidth() - 4
        right = zoom_left - 12
        if self.show_signal_guide:
            label_width = max(self.signal_level_font.measure('↑ ' + label)
                              for label in ('S9+60 dB', '-147 dBm', '255'))
            right -= label_width + 2
        right = max(left + 1, right)
        bottom = max(top + 1, canvas.winfo_height() - bottom_margin)
        self.plot_view = (left, top, right, bottom, now - span, now)

        positions = IC9700_POSITIONS if self.plot_model == 'IC-9700' else STANDARD_POSITIONS
        ticks = METER_TICKS
        reference = None
        axis_title = 'Approx. signal strength  ·  dB above S9'
        if self.plot_model in YAESU_MODELS:
            positions, ticks = RAW_POSITIONS, RAW_TICKS
            axis_title = 'Raw CAT meter level (0–255)'
        elif self.axis_units.get() == 'dBm':
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
        frequency_markers = {}
        last_marker = None
        visible = []
        count = SMOOTHING_WINDOWS[self.smoothing.get()]
        warmup = count - 1
        last_raw = None
        self.measurement_candidates.clear()
        focus = self.comparison[2] if self.comparison is not None else self.focus_frequency
        for reading in reversed(self.history):
            timestamp = reading[0]
            if timestamp > now:
                continue
            if timestamp < now - span:
                if warmup == 0:
                    break
                warmup -= 1
            visible.append(reading)
        for timestamp, raw, hz, callsign in smooth_samples(reversed(visible), count):
            if timestamp < now - span:
                continue
            if focus is not None and hz != focus:
                raw = None
            last_raw = raw
            column = round(right - (now - timestamp) / span * (right - left))
            if raw is None:
                columns.setdefault(column, []).append(None)
                continue
            if self.measuring:
                self.measurement_candidates.append((timestamp, raw, hz, callsign))
            if hz is not None and (hz, callsign) != last_marker:
                changed = last_marker is not None and last_marker[0] != hz
                frequency_markers[column] = hz, changed, callsign
                last_marker = hz, callsign
            segments = columns.setdefault(column, [])
            if not segments or segments[-1] is None or segments[-1][0] != hz:
                segments.append([hz, raw, raw, raw, raw])
            else:
                limits = segments[-1]
                limits[2] = min(limits[2], raw)
                limits[3] = max(limits[3], raw)
                limits[4] = raw

        theme_index = int(theme == 'Night')
        band_colours = {band: colours[theme_index] for band, colours in TRACE_BAND_COLOURS.items()}
        for column, (hz, changed, _) in frequency_markers.items():
            if changed:
                colour = band_colours.get(beacon_band(hz), palette['muted'])
                canvas.create_line(column, top, column, bottom, fill=colour, dash=(3, 4))

        level = None
        if self.show_signal_guide and last_raw is not None:
            position = positions[last_raw]
            height = y(position)
            level = signal_level_label(position, reference, raw_scale=positions is RAW_POSITIONS)
            if height >= top:
                canvas.create_line(left, height, right, height, fill=palette['reference'], dash=(1, 3))
            else:
                level = '↑ ' + level
            label_y = min(bottom - 9, max(top + 9, height))

        def trace_points():
            previous_hz = None
            previous_raw = None
            for column, segments in columns.items():
                for segment in segments:
                    if segment is None:
                        previous_raw = None
                        yield None, None
                        continue
                    hz, first, low, high, last = segment
                    if previous_hz != hz:
                        previous_raw = None
                        yield None, None
                    previous_hz = hz
                    band_colour = band_colours.get(beacon_band(hz))
                    for raw in (first, low, high, last):
                        level_index = raw if previous_raw is None else round((previous_raw + raw) / 2)
                        yield (column, y(positions[raw]), raw), band_colour or trace_colours[level_index]
                        previous_raw = raw

        draw_trace(canvas, trace_points(), top)

        if self.comparison is not None:
            self.draw_comparison(y, count, positions is RAW_POSITIONS)
        elif focus is not None:
            text, = self.frequency_choices({focus})
            canvas.create_text(right, 12, text=text, anchor='e', fill=palette['text'])

        label_right = left
        for column, (hz, _, callsign) in frequency_markers.items():
            frequency = f'{hz / 1_000_000:.6f}'.rstrip('0').rstrip('.')
            colour = band_colours.get(beacon_band(hz), palette['text'])
            text = f'{frequency} MHz'
            if callsign:
                text += f'\n{callsign}'
            label = canvas.create_text(column + 4, top + 3, text=text, anchor='nw', justify='left',
                                       font=('Segoe UI', 9, 'bold'), fill=colour)
            x0, y0, x1, y1 = canvas.bbox(label)
            shift = min(0, right - x1 - 3)
            if x0 + shift < label_right:
                canvas.delete(label)
                continue
            canvas.move(label, shift, 0)
            background = canvas.create_rectangle(x0 + shift, y0, x1 + shift, y1,
                                                 fill=palette['panel'], outline='')
            canvas.tag_lower(background, label)
            label_right = x1 + shift + 6

        self.draw_plot_comments()
        if level is not None:
            label = canvas.create_text(right + 6, label_y, text=level, anchor='w',
                                       font=self.signal_level_font, fill=palette['text'])
            x0, y0, x1, y1 = canvas.bbox(label)
            badge = canvas.create_rectangle(x0 - 2, y0, x1 + 2, y1, fill=palette['panel'],
                                            outline=palette['border'])
            canvas.tag_lower(badge, label)
        self.draw_measurement(y, positions)
        self.update_footer()

    def draw_measurement(self, y, positions):
        left, top, right, bottom, start, end = self.plot_view
        colour = '#006c72' if self.active_theme == 'Day' else '#8bf2e9'
        for name, (stamp, raw, _, _) in zip(('A', 'B'), self.measurement):
            if not start <= stamp <= end:
                continue
            x = left + (stamp - start) / (end - start) * (right - left)
            self.chart.create_line(x, top, x, bottom, fill=colour, dash=(5, 3))
            self.chart.create_text(min(right - 5, x + 7), top + 49, text=name, anchor='w',
                                   fill=colour, font=('Segoe UI', 10, 'bold'))
            height = y(positions[raw])
            if top <= height <= bottom:
                self.chart.create_oval(x - 3, height - 3, x + 3, height + 3, fill=colour, outline='')

    def draw_comparison(self, y, count, raw_scale):
        model, samples, frequency, _ = self.comparison
        left, top, right, _, start, end = self.plot_view
        lo = bisect_left(samples, start, key=lambda sample: sample[0])
        hi = bisect_right(samples, end, key=lambda sample: sample[0])
        if raw_scale:
            positions = RAW_POSITIONS
        else:
            positions = IC9700_POSITIONS if model == 'IC-9700' else STANDARD_POSITIONS
        colour = '#a92146' if self.active_theme == 'Day' else '#ff9eb8'
        columns = []
        for stamp, raw, hz, _ in smooth_samples(samples[max(0, lo - count + 1):hi], count):
            if stamp < start:
                continue
            if raw is None or hz != frequency:
                if columns and columns[-1] is not None:
                    columns.append(None)
                continue
            x = round(left + (stamp - start) / (end - start) * (right - left))
            if columns and columns[-1] is not None and columns[-1][0] == x:
                column = columns[-1]
                column[2] = min(column[2], raw)
                column[3] = max(column[3], raw)
                column[4] = raw
            else:
                columns.append([x, raw, raw, raw, raw])
        previous = None
        for column in columns:
            if column is None:
                previous = None
                continue
            x, *values = column
            for raw in values:
                point = (x, y(positions[raw]))
                if point == previous:
                    continue
                if previous is None and point[1] >= top:
                    self.chart.create_oval(x - 1, point[1] - 1, x + 1, point[1] + 1, fill=colour, outline='')
                elif previous is not None and (segment := visible_trace_segment(previous, point, top)):
                    self.chart.create_line(*segment, fill=colour, dash=(4, 2))
                previous = point
        self.chart.create_text(right, 12, text='Comparison (UTC)', anchor='e', fill=colour)

    def draw_plot_comments(self):
        self.comment_hitboxes.clear()
        if not self.plot_comments:
            return
        left, top, right, bottom, start, end = self.plot_view
        canvas = self.chart
        palette = DISPLAY_THEMES[self.active_theme]
        colour = '#955418' if self.active_theme == 'Day' else '#ffbf66'
        flag_y = min(top + 42, bottom - 5)
        label_right = left
        for stamp, text in sorted(self.plot_comments.items()):
            if not start <= stamp <= end:
                continue
            column = round(left + (stamp - start) / (end - start) * (right - left))
            canvas.create_line(column, flag_y, column, bottom, fill=colour, dash=(2, 4))
            canvas.create_polygon(column, flag_y - 5, column + 4, flag_y,
                                  column, flag_y + 5, column - 4, flag_y, fill=colour)
            label = canvas.create_text(column + 6, flag_y, text=text, anchor='nw',
                                       width=min(230, right - left), font=('Segoe UI', 9), fill=colour)
            x0, y0, x1, y1 = canvas.bbox(label)
            shift = min(0, right - x1 - 3)
            bounds = None
            if y1 <= bottom and x0 + shift >= label_right:
                canvas.move(label, shift, 0)
                bounds = (x0 + shift, y0, x1 + shift, y1)
                background = canvas.create_rectangle(*bounds, fill=palette['panel'], outline='')
                canvas.tag_lower(background, label)
                label_right = bounds[2] + 6
            else:
                canvas.delete(label)
            self.comment_hitboxes.append((stamp, column, bounds))

    def remember_port(self, value):
        if value is None:
            return
        port = self.port.get().strip()
        address = self.address.get().strip() if self.plot_model in RADIO_ADDRESSES else ''
        connection = {'model': self.plot_model, 'port': port, 'baud': self.baud.get(), 'address': address}
        self.radio_bauds[self.plot_model] = self.baud.get()
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
                'radio_bauds': {**self.radio_bauds, self.model.get(): self.baud.get()},
                'last_connected_radio': self.last_connected_radio,
                'beacons': self.beacons,
                'baud': self.baud.get(),
                'address': self.address.get(),
                'sample_interval_seconds': self.sample_interval,
                'smoothing': self.smoothing.get(),
                'axis_units': self.axis_units.get(),
                'show_signal_guide': self.show_signal_guide,
                'lift_alerts': self.lift_alerts,
                'recording_folder': str(self.recording_folder) if self.recording_folder else '',
                'scan_frequencies': self.scan_frequencies,
                'scan_dwell': self.scan_dwell,
                'dbm_reference': self.dbm_reference.get(),
                'plot_transparency': self.plot_transparency,
                'warn_on_close': self.warn_on_close,
                'auto_connect': self.auto_connect,
                'station_callsign': self.station_callsign,
                'station_locator': self.station_locator,
                'google_maps_key': self.google_maps_key,
                'synoptic_source': self.synoptic_source,
                'synoptic_auto_bom': self.synoptic_auto_bom,
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
        if not self.flush_recording() and not messagebox.askyesno(
            'Daily recording', 'The last recording could not be saved. Quit anyway?', parent=self,
        ):
            return
        self.stop_scan()
        if self.stop is not None:
            self.stop.set()
        self.cancel_radio_auto_connect()
        if self.map_server is not None:
            self.map_server.close()
        self.persist_settings()
        self.destroy()


if __name__ == '__main__':
    Plotter().mainloop()
