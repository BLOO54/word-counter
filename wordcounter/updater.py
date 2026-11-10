"""Built-in updater.

Plain English:
1. We ask GitHub "what's the newest release of this app?" (a public web address,
   no account needed).
2. If it's newer than the version running, we tell you.
3. When you click "Update", we download the new installer, check its fingerprint
   (SHA-256) matches what GitHub says so it can't be a damaged or swapped file,
   start it in silent mode, and close the app. The installer replaces the
   program files, keeps your settings, and starts the new version for you.
   It installs for your user account only, so no administrator prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import APP_NAME, APP_VERSION

log = logging.getLogger(__name__)

REPO = "BLOO54/word-counter"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
USER_AGENT = f"{APP_NAME.replace(' ', '')}-Updater/{APP_VERSION}"
CHECK_EVERY_HOURS = 6


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str              # "1.9.0"
    installer_url: str
    installer_name: str
    size: int
    sha256: str | None        # from GitHub's asset "digest", if provided
    notes: str
    page_url: str


def parse_version(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def check_latest(current: str = APP_VERSION, opener=None) -> UpdateInfo | None:
    """Return info about a newer release, or None if we're up to date."""
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(API_LATEST, headers={"User-Agent": USER_AGENT,
                                                      "Accept": "application/vnd.github+json"})
    try:
        with opener(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise UpdateError(f"Couldn't reach GitHub: {e}") from e
    tag = data.get("tag_name") or ""
    if not tag or not is_newer(tag, current):
        return None
    asset = next((a for a in data.get("assets", [])
                  if a.get("name", "").lower().endswith(".exe") and "setup" in a.get("name", "").lower()), None)
    if asset is None:
        raise UpdateError(f"Release {tag} has no installer attached")
    digest = asset.get("digest") or ""
    sha = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else None
    notes = (data.get("body") or "").strip()
    return UpdateInfo(version=tag.lstrip("vV"), installer_url=asset["browser_download_url"],
                      installer_name=asset["name"], size=int(asset.get("size") or 0), sha256=sha,
                      notes=notes, page_url=data.get("html_url") or RELEASES_PAGE)


def whats_new(info: UpdateInfo, limit: int = 160) -> str:
    """First 'What's new' line from the release notes, if there is one."""
    for line in info.notes.splitlines():
        line = line.strip().lstrip("#*- ").strip()
        if line.lower().startswith("what's new"):
            text = line.split(":", 1)[1].strip() if ":" in line else ""
            return text[:limit]
    return ""


def download(info: UpdateInfo, dest_dir: Path | None = None, opener=None, progress=None) -> Path:
    """Download the installer and verify it. Returns the file path."""
    opener = opener or urllib.request.urlopen
    dest_dir = Path(dest_dir or tempfile.gettempdir()) / "WordCounter-update"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / info.installer_name
    tmp = target.with_suffix(".part")
    h = hashlib.sha256()
    got = 0
    req = urllib.request.Request(info.installer_url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(req, timeout=60) as resp, open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress and info.size:
                    progress(min(1.0, got / info.size))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"Download failed: {e}") from e
    if info.size and got != info.size:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"Download incomplete ({got} of {info.size} bytes)")
    if info.sha256 and h.hexdigest().lower() != info.sha256:
        tmp.unlink(missing_ok=True)
        raise UpdateError("Downloaded file doesn't match GitHub's fingerprint - not installing it")
    os.replace(tmp, target)
    return target


def install_mode() -> str:
    """'installed' (can self-update), 'portable' (zip version) or 'source' (running the .py files)."""
    if not getattr(sys, "frozen", False):
        return "source"
    exe_dir = Path(sys.executable).resolve().parent
    if any(exe_dir.glob("unins*.exe")):
        return "installed"
    return "portable"


INSTALLER_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                  "/FORCECLOSEAPPLICATIONS", "/RESTARTAPP=1"]


def launch_installer(path: Path) -> None:
    """Start the installer silently; it restarts the app when it's done."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(path), *INSTALLER_ARGS], close_fds=True, creationflags=flags)
