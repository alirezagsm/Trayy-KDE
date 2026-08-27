import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from trayy import REPO_URL

logger = logging.getLogger("trayy.update")

GITHUB_API_REPO = REPO_URL.replace("https://github.com/", "")


def github_releases_latest_url() -> str:
    return f"https://api.github.com/repos/{GITHUB_API_REPO}/releases/latest"


def _parse_version(v: str) -> tuple[int, ...]:
    parts = [int(x) for x in v.lstrip("vV").split(".") if x.isdigit()]
    return tuple(parts) if parts else (0,)


def needs_update(current: str, latest: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


@dataclass
class ReleaseAsset:
    name: str
    download_url: str
    digest_algo: str
    digest_hex: str
    size: int


@dataclass
class ReleaseInfo:
    tag_name: str
    assets: list[ReleaseAsset]


def _parse_release_json(data: dict) -> Optional[ReleaseInfo]:
    tag = data.get("tag_name", "")
    if not tag:
        return None
    assets = []
    for a in data.get("assets", []):
        digest = a.get("digest", "")
        if ":" in digest:
            algo, hex_hash = digest.split(":", 1)
        else:
            algo, hex_hash = "sha256", ""
        assets.append(ReleaseAsset(
            name=a.get("name", ""),
            download_url=a.get("browser_download_url", ""),
            digest_algo=algo,
            digest_hex=hex_hash,
            size=a.get("size", 0),
        ))
    return ReleaseInfo(tag_name=tag, assets=assets)


async def _curl(url: str, *, timeout: float = 5.0) -> Optional[bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-L",
            "-H", "User-Agent: Trayy-Updater/1.0",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout if proc.returncode == 0 else None
    except Exception as e:
        logger.debug("curl failed: %s", e)
        return None


async def _fetch_latest_release() -> Optional[ReleaseInfo]:
    data = await _curl(github_releases_latest_url())
    if not data:
        return None
    try:
        return _parse_release_json(json.loads(data.decode()))
    except (json.JSONDecodeError, KeyError):
        return None


UPDATE_AVAILABLE: bool = False
LATEST_VERSION: str = ""


async def check_for_updates(current_version: str = "") -> bool:
    global UPDATE_AVAILABLE, LATEST_VERSION

    release = await _fetch_latest_release()
    if not release:
        UPDATE_AVAILABLE = False
        LATEST_VERSION = ""
        return False

    latest = release.tag_name
    LATEST_VERSION = latest
    UPDATE_AVAILABLE = needs_update(current_version, latest)

    if UPDATE_AVAILABLE:
        logger.info("update available: %s → %s", current_version, latest)
    return UPDATE_AVAILABLE


async def download_release_asset(
    asset_name: str,
    output_path: str,
    *,
    timeout: float = 60.0,
) -> bool:
    release = await _fetch_latest_release()
    if not release:
        logger.error("could not fetch release info for download")
        return False

    target = next((a for a in release.assets if a.name == asset_name), None)
    if not target:
        logger.error("asset '%s' not found in release %s", asset_name, release.tag_name)
        return False

    logger.info("downloading %s from release %s", asset_name, release.tag_name)

    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-L", "-o", output_path,
            "-H", "User-Agent: Trayy-Updater/1.0",
            target.download_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            logger.error("curl failed (rc=%d): %s", proc.returncode, stderr.decode().strip())
            return False
    except FileNotFoundError:
        logger.error("curl not found")
        return False
    except asyncio.TimeoutError:
        logger.error("download timed out after %.0fs", timeout)
        return False
    except Exception as e:
        logger.error("download failed: %s", e)
        return False

    # Verify hash
    if not target.digest_hex:
        logger.error("no digest published for %s — refusing to install unverified release", asset_name)
        return False

    hash_fn = getattr(hashlib, target.digest_algo.lower(), None)
    if hash_fn is None:
        logger.error("unsupported digest algorithm: %s — refusing to install unverified release", target.digest_algo)
        return False

    try:
        h = hash_fn()
        with open(output_path, "rb") as f:
            h.update(f.read())
    except OSError as e:
        logger.error("failed to read downloaded file: %s", e)
        return False

    computed = h.hexdigest()
    expected = target.digest_hex.lower()
    if computed != expected:
        logger.error("hash mismatch for %s: expected %s:%s, got %s:%s",
                     asset_name, target.digest_algo, expected, target.digest_algo, computed)
        return False

    logger.info("hash verified for %s (%s)", asset_name, target.digest_algo)
    return True


RELEASE_ZIP_NAME = "Trayy.zip"
UPDATE_FILES = ("trayy.pyz", "trayyd")


def _find_member(zf: zipfile.ZipFile, name: str) -> Optional[str]:
    """Find a zip member by basename (release zips may nest files in a folder)."""
    for member in zf.namelist():
        if member.endswith("/" + name) or member == name:
            return member
    return None


async def apply_update(
    install_dir: str,
    *,
    progress_cb=None,
    timeout: float = 120.0,
) -> tuple[bool, str]:
    """Download the latest release zip and replace trayy.pyz/trayyd in install_dir.

    The .config file is left untouched. Returns (success, message).
    """
    install = Path(install_dir)
    tmp_dir = Path(tempfile.mkdtemp(prefix="trayy-update-"))
    zip_path = tmp_dir / RELEASE_ZIP_NAME
    try:
        if progress_cb:
            progress_cb(f"Downloading {RELEASE_ZIP_NAME}…")
        ok = await download_release_asset(RELEASE_ZIP_NAME, str(zip_path), timeout=timeout)
        if not ok:
            return False, f"failed to download {RELEASE_ZIP_NAME}"

        if progress_cb:
            progress_cb("Extracting…")
        try:
            zf = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile:
            return False, "downloaded file is not a valid zip archive"

        with zf:
            for name in UPDATE_FILES:
                member = _find_member(zf, name)
                if member is None:
                    return False, f"'{name}' not found in {RELEASE_ZIP_NAME}"
                dest = install / name
                # Write to a temp file in the same directory, then atomically
                # replace — avoids a half-written binary if we're interrupted.
                tmp_dest = dest.with_suffix(dest.suffix + ".new")
                with zf.open(member) as src, open(tmp_dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(tmp_dest, 0o755)
                os.replace(tmp_dest, dest)
                logger.info("updated %s", dest)

        return True, "update applied"
    except Exception as e:
        logger.error("update failed: %s", e)
        return False, f"update failed: {e}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
