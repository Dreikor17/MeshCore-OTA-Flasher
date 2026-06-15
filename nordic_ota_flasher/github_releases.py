"""
Fetch the latest MeshCore firmware (.zip) and the latest OTAFIX bootloader (.uf2)
from GitHub Releases.

Notes baked in from research:
  * The repeater/room-server firmware is NOT returned by /releases/latest (that endpoint
    only surfaces the companion stream), so we list releases and match by tag prefix.
  * Release tags: companion-vX.Y.Z | repeater-vX.Y.Z | room-server-vX.Y.Z
  * RAK4631 BLE-OTA artifact = the .zip; the .uf2 is USB-only.
  * Actual file downloads use browser_download_url (a CDN) and are NOT API-rate-limited.
    Set GITHUB_TOKEN in the environment to raise the listing limit from 60 to 5000/hr.

These functions are synchronous; call them from the GUI via asyncio.to_thread().
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

API = "https://api.github.com"
MESHCORE_REPO = "meshcore-dev/MeshCore"
OTAFIX_REPO = "oltaco/Adafruit_nRF52_Bootloader_OTAFIX"

# role -> regex that the .zip asset name must match (board + role)
ROLE_ASSET_PATTERNS = {
    "repeater": r"^RAK_4631_repeater-.*\.zip$",
    "companion": r"^RAK_4631_companion_radio_ble-.*\.zip$",
    "room-server": r"^RAK_4631_room_server-.*\.zip$",
}

OTAFIX_UF2_PATTERN = r"^update-wiscore_rak4631_board_bootloader-.*_nosd\.uf2$"

# The BLE-OTA-flashable OTAFIX bootloader DFU packages (combined SoftDevice+Bootloader),
# one per board, e.g. wiscore_rak4631_board_bootloader-...-_s140_6.1.1.zip
OTAFIX_BL_ZIP_PATTERN = r"_s140_.*\.zip$"


class GitHubError(Exception):
    pass


@dataclass
class Asset:
    name: str
    url: str
    size: int
    tag: str
    published_at: str = ""


# GitHub API errors that are transient (its gateway/server hiccupped or throttled) and worth
# retrying — a 504 Gateway Time-out is the common one.
_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})


def _get(url: str, retries: int = 3) -> object:
    headers = {
        "User-Agent": "nordic-ota-flasher",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        last = attempt == retries - 1
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise GitHubError(
                    "GitHub API rate limit hit (60/hr unauthenticated). "
                    "Set a GITHUB_TOKEN environment variable to raise it."
                )
            if e.code in _RETRYABLE_HTTP and not last:
                time.sleep(2 ** attempt)  # 1 s, 2 s backoff
                continue
            if e.code in _RETRYABLE_HTTP:
                raise GitHubError(
                    f"GitHub API temporarily unavailable ({e.code} {e.reason}) after "
                    f"{retries} tries — try again in a moment."
                )
            raise GitHubError(f"GitHub API error {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            if not last:
                time.sleep(2 ** attempt)
                continue
            raise GitHubError(f"Network error contacting GitHub: {e.reason}")
    raise GitHubError("GitHub API unreachable after retries.")  # unreachable in practice


def latest_meshcore_firmware(role: str = "repeater") -> Asset:
    """Return the newest RAK4631 .zip asset for the given MeshCore role."""
    pattern = ROLE_ASSET_PATTERNS.get(role)
    if pattern is None:
        raise GitHubError(f"Unknown role '{role}'. Choose one of {list(ROLE_ASSET_PATTERNS)}.")
    rx = re.compile(pattern)
    releases = _get(f"{API}/repos/{MESHCORE_REPO}/releases?per_page=40")
    for rel in releases:  # GitHub returns newest first
        tag = rel.get("tag_name", "")
        if not tag.startswith(role + "-"):
            continue
        for a in rel.get("assets", []):
            if rx.match(a["name"]):
                return Asset(
                    name=a["name"],
                    url=a["browser_download_url"],
                    size=a["size"],
                    tag=tag,
                    published_at=rel.get("published_at", ""),
                )
    raise GitHubError(f"No RAK4631 '{role}' .zip found in recent MeshCore releases.")


def latest_otafix_bootloader() -> Asset:
    """Return the newest OTAFIX RAK4631 bootloader .uf2 (USB-install file)."""
    rx = re.compile(OTAFIX_UF2_PATTERN)
    rel = _get(f"{API}/repos/{OTAFIX_REPO}/releases/latest")
    for a in rel.get("assets", []):
        if rx.match(a["name"]):
            return Asset(
                name=a["name"],
                url=a["browser_download_url"],
                size=a["size"],
                tag=rel.get("tag_name", ""),
                published_at=rel.get("published_at", ""),
            )
    raise GitHubError("No RAK4631 OTAFIX bootloader .uf2 found in the latest release.")


def _otafix_board_label(asset_name: str) -> str:
    """'wiscore_rak4631_board_bootloader-0.9.2-..._s140_6.1.1.zip' -> 'wiscore_rak4631_board'."""
    return asset_name.split("_bootloader-")[0]


def list_otafix_bootloader_zips() -> list[tuple[str, Asset]]:
    """Return (board_label, Asset) for every BLE-OTA-flashable OTAFIX bootloader DFU zip
    in the latest release, RAK boards sorted first."""
    rx = re.compile(OTAFIX_BL_ZIP_PATTERN)
    rel = _get(f"{API}/repos/{OTAFIX_REPO}/releases/latest")
    tag = rel.get("tag_name", "")
    out: list[tuple[str, Asset]] = []
    for a in rel.get("assets", []):
        if rx.search(a["name"]):
            out.append(
                (
                    _otafix_board_label(a["name"]),
                    Asset(a["name"], a["browser_download_url"], a["size"], tag),
                )
            )
    out.sort(key=lambda x: (not x[0].lower().startswith(("wiscore_rak", "rak")), x[0].lower()))
    return out


def download(url: str, dest: str, progress=None) -> str:
    """Stream a URL to dest. progress(received_bytes, total_bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": "nordic-ota-flasher"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0) or 0)
        received = 0
        with open(dest, "wb") as f:
            while True:
                buf = resp.read(64 * 1024)
                if not buf:
                    break
                f.write(buf)
                received += len(buf)
                if progress:
                    progress(received, total)
    return dest
