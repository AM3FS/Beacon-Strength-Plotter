"""Decode Maidenhead locators and look up countries using local map data."""

from functools import lru_cache
import json
import math
from pathlib import Path
import re


LOCATOR_PATTERN = re.compile(r'[A-R]{2}[0-9]{2}[A-X]{2}(?:[0-9]{2})?')
COUNTRY_DATA_PATH = Path(__file__).with_name('station_countries.json')


def maidenhead_coordinates(locator, *, allow_four=False):
    """Return the grid cell centre as (latitude, longitude) in decimal degrees."""
    locator = locator.strip().upper()
    if allow_four and re.fullmatch(r'[A-R]{2}[0-9]{2}', locator):
        lon = -180 + 20 * (ord(locator[0]) - ord('A')) + 2 * int(locator[2])
        lat = -90 + 10 * (ord(locator[1]) - ord('A')) + int(locator[3])
        return lat + 0.5, lon + 1
    if not LOCATOR_PATTERN.fullmatch(locator):
        raise ValueError('Enter a 6- or 8-character Maidenhead locator, such as QF22AA or QF22AA12.')

    lon = -180 + 20 * (ord(locator[0]) - ord('A')) + 2 * int(locator[2])
    lat = -90 + 10 * (ord(locator[1]) - ord('A')) + int(locator[3])
    width, height = 2 / 24, 1 / 24
    lon += width * (ord(locator[4]) - ord('A'))
    lat += height * (ord(locator[5]) - ord('A'))
    if len(locator) == 8:
        width /= 10
        height /= 10
        lon += width * int(locator[6])
        lat += height * int(locator[7])
    return lat + height / 2, lon + width / 2


def beacon_path(origin, destination):
    """Return great-circle kilometres and initial true bearing in degrees."""
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    delta_lon = lon2 - lon1
    haversine = (math.sin((lat2 - lat1) / 2) ** 2
                 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2)
    distance = 12_742.0176 * math.asin(math.sqrt(min(1, haversine)))
    if min(haversine, 1 - haversine) < 1e-15:
        return distance, None
    x = math.sin(delta_lon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    return distance, math.degrees(math.atan2(x, y)) % 360


@lru_cache(maxsize=1)
def country_polygons():
    with COUNTRY_DATA_PATH.open(encoding='utf-8') as source:
        return json.load(source)


def ring_contains(ring, lon, lat):
    inside = False
    previous_lon, previous_lat = ring[-1]
    for current_lon, current_lat in ring:
        if (previous_lat > lat) != (current_lat > lat):
            crossing = previous_lon + (current_lon - previous_lon) * (
                (lat - previous_lat) / (current_lat - previous_lat)
            )
            if lon < crossing:
                inside = not inside
        previous_lon, previous_lat = current_lon, current_lat
    return inside


def country_at(latitude, longitude):
    """Return a Natural Earth country or territory name, or None at sea."""
    for name, bounds, rings in country_polygons():
        west, south, east, north = bounds
        if (west <= longitude <= east and south <= latitude <= north
                and ring_contains(rings[0], longitude, latitude)
                and not any(ring_contains(hole, longitude, latitude) for hole in rings[1:])):
            return name
    return None
