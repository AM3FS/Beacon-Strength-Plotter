"""Serve the beacon path locally for Google Maps, or export it as KML."""

from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from secrets import token_urlsafe
from threading import Thread
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from station_location import beacon_path

MAP_HOST = '127.0.0.1'
MAP_PORT = 8767
MAP_ORIGIN = f'http://{MAP_HOST}:{MAP_PORT}'
MY_MAPS_URL = 'https://www.google.com/mymaps'


def map_page(api_key, station, beacon, origin, destination, summary, *, elevation=False):
    data = json.dumps(dict(
        station=station, beacon=beacon, summary=summary,
        origin=dict(lat=origin[0], lng=origin[1]),
        destination=dict(lat=destination[0], lng=destination[1]),
        distance=beacon_path(origin, destination)[0] if elevation else 0,
        elevation=elevation,
    )).replace('<', '\\u003c')
    query = urlencode({'key': api_key, 'callback': 'initMap', 'loading': 'async'})
    return ('''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Beacon path</title>
<style>
body{margin:0;font:16px system-ui,sans-serif;color:#192b3c;background:#edf1f5}
main{display:grid;grid-template-rows:auto minmax(16rem,1fr) auto;min-height:100dvh}
header{padding:12px 18px} h1{font-size:1.25rem;margin:0 0 4px}
p{margin:0} #map{height:100%;min-height:10rem}
#profile{padding:12px 18px;background:#fff;min-height:calc(clamp(160px,28vh,280px) + 46px)}
#profile h2{font-size:1rem;margin:0 0 4px}
#profile svg{display:block;width:100%;height:clamp(160px,28vh,280px)}
</style></head><body><main><header><h1 id="title"></h1>
<p id="details" role="status">Loading Google Maps…</p></header>
<div id="map" aria-label="Station to beacon map"></div>
<section id="profile" hidden><h2>Elevation profile</h2>
<p id="elevation-status" role="status"></p></section></main><script>
const path = ''' + data + ''';
const details = document.getElementById('details');
document.getElementById('title').textContent = path.station + ' → ' + path.beacon;
function mapError() {
  details.textContent = 'Google Maps could not load. Check the API key, billing and website restriction.';
}
window.gm_authFailure = mapError;

function elevationError(error, step) {
  // Display only known codes. Provider error messages can include request URLs.
  const knownCode = /\\b(REQUEST_DENIED|PERMISSION_DENIED|OVER_QUERY_LIMIT|OVER_DAILY_LIMIT|RESOURCE_EXHAUSTED|INVALID_REQUEST|INVALID_ARGUMENT|UNKNOWN_ERROR|UNAVAILABLE|DEADLINE_EXCEEDED|TypeError|ReferenceError|NetworkError)\\b/;
  const values = [error?.code, error?.message, error?.name, error];
  for (const value of values) {
    if (typeof value !== 'string') continue;
    const match = value.match(knownCode);
    if (match) return 'Elevation failed while ' + step + ' (' + match[1] + ').';
  }
  return 'Elevation failed while ' + step + '. Google supplied no recognised error code.';
}

async function showElevation() {
  const section = document.getElementById('profile');
  const status = document.getElementById('elevation-status');
  section.hidden = false;
  if (path.distance === 0) {
    status.textContent = 'The station and beacon have the same coordinates.';
    return;
  }
  status.textContent = 'Loading ground elevation…';
  let step = 'loading the Google elevation library';
  try {
    const {ElevationService} = await google.maps.importLibrary('elevation');
    step = 'requesting ground heights from Google';
    const {results} = await new ElevationService().getElevationAlongPath({
      path: [path.origin, path.destination],
      samples: Math.min(256, Math.max(32, Math.ceil(path.distance) + 1))
    });
    step = 'reading the elevation response';
    const heights = results.map(point => point.elevation);
    if (heights.length < 2 || !heights.every(Number.isFinite)) {
      status.textContent = 'Google returned no usable elevation profile for this path.';
      return;
    }
    step = 'drawing the elevation profile';
    drawElevation(section, heights);
    status.textContent = 'Station ' + Math.round(heights[0]) + ' m · Beacon ' +
      Math.round(heights.at(-1)) + ' m · Highest sample ' + Math.round(Math.max(...heights)) + ' m';
  } catch (error) {
    const message = elevationError(error, step);
    status.textContent = message;
    console.warn(message);
  }
}

function drawElevation(section, heights) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 800 215');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'Ground elevation in metres along the station-to-beacon path in kilometres');
  section.append(svg);
  const left = 62, right = 764, top = 16, bottom = 168;
  const low = Math.floor(Math.min(...heights) / 10) * 10;
  const high = Math.max(low + 10, Math.ceil(Math.max(...heights) / 10) * 10);
  const x = fraction => left + fraction * (right - left);
  const y = amount => bottom - (amount - low) / (high - low) * (bottom - top);
  function element(tag, attrs, label) {
    const node = document.createElementNS(ns, tag);
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
    if (label !== undefined) node.textContent = label;
    svg.append(node);
  }
  for (let tick = 0; tick <= 4; tick++) {
    const fraction = tick / 4;
    const elevation = low + fraction * (high - low);
    const height = y(elevation);
    element('line', {x1: left, x2: right, y1: height, y2: height, stroke: '#cbd5df'});
    element('text', {x: left-8, y: height+4, 'text-anchor': 'end', 'font-size': 12},
            Math.round(elevation) + ' m');
    element('text', {x: x(fraction), y: bottom+19, 'text-anchor': 'middle', 'font-size': 12},
            (fraction * path.distance).toFixed(path.distance < 10 ? 1 : 0) + ' km');
  }
  const points = heights.map((height, i) => [x(i / (heights.length - 1)), y(height)].join(','));
  element('polygon', {points: [left+','+bottom, ...points, right+','+bottom].join(' '), fill: '#cae3f4'});
  element('polyline', {points: points.join(' '), fill: 'none', stroke: '#17648d', 'stroke-width': 2.5});
  element('text', {x: (left+right)/2, y: 207, 'text-anchor': 'middle', 'font-size': 13},
          'Distance from station');
}

window.initMap = () => {
  if (path.elevation) document.getElementById('profile').hidden = false;
  const map = new google.maps.Map(document.getElementById('map'), {
    center: path.origin, zoom: 9, mapTypeId: path.elevation ? 'terrain' : 'roadmap',
    mapTypeControl: false, streetViewControl: false
  });
  new google.maps.Polyline({
    map, path: [path.origin, path.destination], geodesic: true,
    strokeColor: '#a51d63', strokeOpacity: 1, strokeWeight: 4
  });
  if (path.origin.lat !== path.destination.lat || path.origin.lng !== path.destination.lng) {
    const wide = Math.abs(path.origin.lng - path.destination.lng) > 180;
    const low = Math.min(path.origin.lng, path.destination.lng);
    const high = Math.max(path.origin.lng, path.destination.lng);
    map.fitBounds({
      south: Math.min(path.origin.lat, path.destination.lat),
      north: Math.max(path.origin.lat, path.destination.lat),
      west: wide ? high : low, east: wide ? low : high
    }, 90);
  }
  for (const [position, title] of [[path.origin, path.station], [path.destination, path.beacon]]) {
    const label = document.createElement('span');
    label.textContent = title;
    new google.maps.InfoWindow({position, content: label, disableAutoPan: true}).open({map, shouldFocus: false});
  }
  details.textContent = path.summary;
  if (path.elevation) showElevation();
};
</script><script async src="https://maps.googleapis.com/maps/api/js?''' +
            query.replace('&', '&amp;') + '''" onerror="mapError()"></script></body></html>''').encode('utf-8')


class BeaconMapServer:
    def __init__(self):
        self.pages = OrderedDict()
        maps = self.pages

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                page = maps.get(self.path) if self.headers.get('Host') == f'{MAP_HOST}:{MAP_PORT}' else None
                if page is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(page)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer((MAP_HOST, MAP_PORT), Handler)
        Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True).start()

    def publish(self, page):
        path = '/' + token_urlsafe(24)
        self.pages[path] = page
        if len(self.pages) > 20:
            self.pages.popitem(last=False)
        return f'{MAP_ORIGIN}{path}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def write_path_kml(path, station, beacon, origin, destination, summary):
    def coordinate(location):
        return f'{location[1]:.8f},{location[0]:.8f},0'

    root = ET.Element('kml', xmlns='http://www.opengis.net/kml/2.2')
    document = ET.SubElement(root, 'Document')
    title = f'{station} to {beacon}'
    ET.SubElement(document, 'name').text = title
    for name, location in ((station, origin), (beacon, destination)):
        pin = ET.SubElement(document, 'Placemark')
        ET.SubElement(pin, 'name').text = name
        ET.SubElement(ET.SubElement(pin, 'Point'), 'coordinates').text = coordinate(location)
    line = ET.SubElement(document, 'Placemark')
    ET.SubElement(line, 'name').text = title
    ET.SubElement(line, 'description').text = summary
    path_element = ET.SubElement(line, 'LineString')
    ET.SubElement(path_element, 'tessellate').text = '1'
    ET.SubElement(path_element, 'coordinates').text = f'{coordinate(origin)} {coordinate(destination)}'
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
