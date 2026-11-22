"""Read beacon records from VKSpotter's signed-in page or clipboard export."""

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from io import StringIO
import re
from unicodedata import normalize
from urllib.parse import urlsplit

HOME = 'https://vkspotter.com/'
BEACONS_URL = HOME + '?action=beacons'
CELL_MARKS = str.maketrans('', '', '\u200b\u200e\u200f\ufeff')


def beacon_rows(table):
    fields = {'callsign': 'callsign', 'grid': 'grid', 'qth': 'location',
              'region': 'region', 'freq': 'frequency', 'status': 'status',
              'report date': 'report_date'}
    required = {'callsign', 'grid', 'freq', 'status'}
    columns = {}
    for row in table:
        row = [value.translate(CELL_MARKS).strip() for value in row]
        headers = [' '.join(value.split()).casefold() for value in row]
        if required.issubset(headers):
            columns = {field: headers.index(header) for header, field in fields.items() if header in headers}
        elif columns and all(columns[fields[header]] < len(row) for header in required):
            yield {field: row[index] for field, index in columns.items() if index < len(row)}


def clipboard_beacons(text):
    return list(beacon_rows(csv.reader(StringIO(text), delimiter='\t')))


def listed_frequency_hz(value):
    """Read a positive frequency in MHz from a VKSpotter table cell."""
    value = normalize('NFKC', value).translate(CELL_MARKS).strip()
    match = re.fullmatch(
        r'([0-9]{1,6}|[0-9]{1,3}(?:,[0-9]{3})+)(?:([.][0-9]+))?\s*(?:MHz)?',
        value, re.IGNORECASE,
    )
    if not match:
        return None
    mhz = Decimal(match[1].replace(',', '') + (match[2] or ''))
    hz = round(mhz * 1_000_000)
    return hz if 0 < hz < 1_000_000_000_000 else None


@dataclass
class BeaconFetch:
    signed_in: bool = False
    rows: list = field(default_factory=list)
    error: str = ''


# DataTables can paginate records that are present in the browser but absent
# from the current DOM page. Read its data when available.
READ_TABLE = r"""
async (callsign) => {
    const normalize = value => value.trim().replace(/\s+/g, ' ').toLowerCase();
    const required = ['callsign', 'grid', 'freq', 'status'];
    for (const table of document.querySelectorAll('table')) {
        const cells = row => Array.from(row.cells, c => (c.innerText || c.textContent).trim());
        const headers = Array.from(table.tHead?.rows || [], cells)
            .find(row => required.every(name => row.map(normalize).includes(name)));
        if (!headers) continue;
        const jq = window.jQuery;
        if (!jq?.fn?.dataTable?.isDataTable(table)) {
            return [headers, ...Array.from(table.tBodies).flatMap(body => Array.from(body.rows, cells))];
        }
        const api = jq(table).DataTable();
        if (callsign) {
            await new Promise(resolve => {
                const timeout = setTimeout(resolve, 7000);
                api.one('draw', () => { clearTimeout(timeout); resolve(); });
                api.search(callsign).draw();
            });
        }
        const text = (html, _row, _col, node) => {
            if (node) {
                const visible = (node.innerText || node.textContent).trim();
                if (visible) return visible;
            }
            if (html?.nodeType) return html.textContent.trim();
            const template = document.createElement('template');
            template.innerHTML = String(html ?? '');
            return template.content.textContent.trim();
        };
        if (typeof api.buttons?.exportData === 'function') {
            const exported = api.buttons.exportData({
                modifier: {search: 'applied', selected: null},
                format: {header: (html, index, node) => text(html, index, 0, node), body: text}
            });
            return [exported.header, ...exported.body];
        }
        return [headers, ...api.rows({search: 'applied'}).indexes().toArray().map(index =>
            headers.map((_, col) => {
                const cell = api.cell(index, col);
                return text(cell.render('display'), index, col, cell.node());
            }))];
    }
    return [];
}
"""

AUTHENTICATED = r"""
username => {
    const password = document.querySelector('input[name="password"]');
    if (password && password.getClientRects().length) return false;
    const nav = document.querySelectorAll('nav a, .navbar a, .main-header a');
    return Array.from(nav).some(link => {
        const text = link.textContent.trim().toLowerCase();
        const href = link.getAttribute('href') || '';
        return text === username.toLowerCase() || /\b(?:logout|signout)\b/i.test(href)
            || /^(?:log|sign)\s*out$/i.test(text);
    });
}
"""

SIGN_IN_DESTINATION = """
button => {
    const form = button.form;
    const method = button.getAttribute('formmethod') ?? form.getAttribute('method');
    if (method?.toLowerCase() !== 'post') return '';
    const action = button.getAttribute('formaction') ?? form.getAttribute('action');
    const doc = form.ownerDocument;
    return action ? new URL(action, doc.baseURI).href : doc.URL;
}
"""


def trusted_site(url):
    parts = urlsplit(url)
    return parts.scheme == 'https' and parts.netloc in ('vkspotter.com', 'www.vkspotter.com')


def fetch_beacons(username, password, *, callsign=''):
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError:
        return BeaconFetch(error='The VKSpotter browser component is missing. Use Paste Beacon List or rebuild the app.')

    result = BeaconFetch()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', timeout=12000)
            page = browser.new_page()
            page.set_default_timeout(12000)
            page.goto(BEACONS_URL, wait_until='domcontentloaded')
            if not trusted_site(page.url):
                return BeaconFetch(error='VKSpotter redirected outside its HTTPS site.')
            login = page.locator('form').filter(has=page.locator('input[name="password"]'))
            login.wait_for()
            submit = login.locator('[type="submit"]')
            if not trusted_site(submit.evaluate(SIGN_IN_DESTINATION)):
                return BeaconFetch(error='VKSpotter sign-in did not point to an HTTPS form on vkspotter.com.')
            login.locator('[name="username"]').fill(username)
            login.locator('[name="password"]').fill(password)
            submit.click()
            try:
                page.wait_for_function(AUTHENTICATED, arg=username, timeout=8000)
            except TimeoutError:
                return BeaconFetch(error='VKSpotter sign-in could not be confirmed. Check your login or try Paste Beacon List.')
            if not trusted_site(page.url):
                return BeaconFetch(error='VKSpotter redirected outside its HTTPS site.')
            result.signed_in = True
            page.goto(BEACONS_URL, wait_until='domcontentloaded')
            if not trusted_site(page.url) or page.locator('input[name="password"]').first.is_visible():
                return BeaconFetch(error='VKSpotter asked you to sign in again.')
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('table')).some(
                    t => /callsign/i.test(t.tHead?.textContent || '') &&
                    (t.querySelector('tbody td:not([colspan])') ||
                     (window.jQuery?.fn?.dataTable?.isDataTable(t) &&
                      window.jQuery(t).DataTable().rows().any())))""",
                timeout=12000,
            )
            result.rows = list(beacon_rows(page.evaluate(READ_TABLE, callsign)))
            if not result.rows:
                result.error = (f'VKSpotter returned no rows for {callsign}. Try searching the site or pasting its list.'
                                if callsign else 'VKSpotter returned no beacon rows. Try Paste Beacon List.')
    except (Error, OSError, RuntimeError):
        result.error = ('Signed in, but the beacon list could not be read. Try Paste Beacon List.' if result.signed_in
                        else 'Could not sign in using Microsoft Edge. Check your connection or try Paste Beacon List.')
    return result
