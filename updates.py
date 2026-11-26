"""Look up the latest stable Windows installer on GitHub."""

import json
import re
from urllib.request import Request, urlopen


APP_VERSION = '1.0.9'
REPOSITORY = 'AM3FS/Beacon-Strength-Plotter'
RELEASES_URL = f'https://github.com/{REPOSITORY}/releases'
LATEST_RELEASE_API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
INSTALLER_NAME = 'VK3FS-Beacon-Strength-Plotter-Setup.exe'


def version_tuple(version):
    match = re.fullmatch(r'[vV]?(\d+)\.(\d+)\.(\d+)', version)
    if not match:
        raise ValueError('Expected a stable version such as v1.0.0.')
    return tuple(map(int, match.groups()))


def check_latest_release():
    """Return (version, installer URL) for a newer stable release, or None."""
    request = Request(LATEST_RELEASE_API, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': f'VK3FS-Beacon-Strength-Plotter/{APP_VERSION}',
    })
    with urlopen(request, timeout=10) as response:
        release = json.load(response)
    tag = release['tag_name']
    if release.get('draft') or release.get('prerelease'):
        return None
    version = version_tuple(tag)
    if version <= version_tuple(APP_VERSION):
        return None
    installer_url = f'{RELEASES_URL}/download/{tag}/{INSTALLER_NAME}'
    if not any(asset.get('name') == INSTALLER_NAME and asset.get('browser_download_url') == installer_url
               for asset in release.get('assets', ())):
        raise ValueError(f'{tag} is available, but its Windows installer is missing.')
    return tag, installer_url
