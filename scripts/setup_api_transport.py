"""Install checksum-pinned Windows API wheels into this project's private runtime."""
from __future__ import annotations
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PINS = (
    ("curl_cffi", "0.17.0", "curl_cffi-0.17.0-cp310-abi3-win_amd64.whl", "4c3993c7df155d622869b82a56adb018440587fd3faffeaf8d01cc016d2e1357"),
    ("cffi", "2.1.1", "cffi-2.1.1-cp314-cp314-win_amd64.whl", "3222ba5d678f80a030e6afbcc33dc1ae5cb45facabb61cee2c7016b8432fde48"),
    ("pycparser", "3.12", "pycparser-3.12-py3-none-any.whl", "a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e"),
    ("certifi", "2026.10.23", "certifi-2026.10.23-py3-none-any.whl", "e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1"),
    ("tzdata", "2026.5", "tzdata-2026.5-py2.py3-none-any.whl", "b683bd1b6659ddcd810ff02ad09ba821d4bf1065072805063eb35c49617905ac"),
)


def validate_member(name, directory):
    pure = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or pure.is_absolute()
            or ".." in pure.parts or not (directory / name).resolve().is_relative_to(directory.resolve())):
        raise ValueError("Invalid wheel member path")


def download(opener, url, limit):
    if not url.startswith(("https://pypi.org/", "https://files.pythonhosted.org/")):
        raise ValueError("Unexpected package download host")
    with opener.open(url, timeout=40) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("Package download exceeds its size limit")
    return body


def smoke(directory):
    sys.path.insert(0, str(directory))
    try:
        curl = importlib.import_module("curl_cffi")
        from curl_cffi.requests.impersonate import BrowserType
        if curl.__version__ != "0.17.0" or "chrome152" not in {v.value for v in BrowserType}:
            raise ValueError("Unexpected API transport version/profile")
        importlib.import_module("cffi")
        importlib.import_module("certifi")
        importlib.import_module("tzdata")
        from zoneinfo import ZoneInfo
        from datetime import datetime
        zone = ZoneInfo("America/New_York")
        if (datetime(2026, 1, 15, tzinfo=zone).utcoffset().total_seconds() != -18000
                or datetime(2026, 7, 15, tzinfo=zone).utcoffset().total_seconds() != -14400):
            raise ValueError("New York timezone/DST data did not validate")
    finally:
        sys.path.remove(str(directory))


def main():
    if os.name != "nt" or sys.version_info[:2] != (3, 14) or sys.maxsize <= 2**32:
        raise ValueError("Run Setup-API.cmd with the project's Windows x64 Python 3.14 runtime")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    destination = runtime / "api-libs"
    expected = [{"name":n, "version":v, "wheel":w, "sha256":h} for n,v,w,h in PINS]
    install_pins = PINS
    additive = False
    if destination.exists():
        try:
            manifest = json.loads((destination / ".pins.json").read_text(encoding="utf8"))
        except (OSError, ValueError):
            raise ValueError("Existing API runtime is incomplete; preserve/rename .runtime/api-libs before setup") from None
        if manifest == expected:
            smoke(destination)
            print("Private API transport is ready: curl_cffi 0.17.0, Chrome 152, direct connections and New York DST data.")
            return
        if manifest == expected[:-1]:
            # One recognized additive upgrade. Preserve all installed transport
            # files; install only the newly pinned timezone package.
            install_pins = PINS[-1:]
            additive = True
        else:
            raise ValueError("Existing API runtime has different pins; preserve/rename .runtime/api-libs before setup")
    stage = runtime / ("api-libs-install-" + uuid.uuid4().hex)
    stage.mkdir()
    archives = runtime / "api-wheels"
    archives.mkdir(exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for name, version, filename, digest in install_pins:
        archive = archives / filename
        candidates = (archive, runtime / "native-analysis" / filename)
        found = next((p for p in candidates if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == digest), None)
        if found:
            if found != archive:
                shutil.copyfile(found, archive)
        else:
            metadata = json.loads(download(opener, f"https://pypi.org/pypi/{name}/{version}/json", 2_000_000))
            records = [r for r in metadata["urls"] if r["filename"] == filename and r["digests"]["sha256"] == digest]
            if len(records) != 1:
                raise ValueError("The pinned wheel is missing or its checksum changed")
            payload = download(opener, records[0]["url"], 16_000_000)
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("Package checksum mismatch")
            archive.write_bytes(payload)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise ValueError("Cached package checksum mismatch")
        with zipfile.ZipFile(archive) as wheel:
            if sum(m.file_size for m in wheel.infolist()) > 100_000_000:
                raise ValueError("Wheel expands beyond its size limit")
            for member in wheel.infolist():
                validate_member(member.filename, stage)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Wheel contains a symbolic link")
            wheel.extractall(stage)
        print(f"Verified private dependency: {name} {version}", flush=True)
    if additive:
        for child in stage.iterdir():
            if (destination / child.name).exists():
                raise ValueError("The new timezone package already exists without a completed manifest")
        for child in stage.iterdir():
            child.rename(destination / child.name)
        smoke(destination)
        (destination / ".pins.json").write_text(json.dumps(expected, indent=2), encoding="utf8")
        stage.rmdir() # Only the now-empty directory created by this setup.
    else:
        (stage / ".pins.json").write_text(json.dumps(expected, indent=2), encoding="utf8")
        smoke(stage)
        stage.rename(destination)
    print("Private API transport installed. No global packages or proxy settings changed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ValueError) else "Package download, extraction or import failed."
        print("API transport setup stopped: " + detail, file=sys.stderr)
        sys.exit(1)
