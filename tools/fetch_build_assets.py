"""Fetch only inventoried public build helpers; verify every binary before use."""

import argparse
import hashlib
import http.client
import json
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from udb.components import verified_assets


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, archive, *, opener=urllib.request.urlopen, pause=time.sleep):
    if archive.exists() or archive.is_symlink():
        raise FileExistsError("Download destination already exists")
    for attempt in range(3):
        created = False
        try:
            with opener(url, timeout=120) as response:
                with archive.open("xb") as out:
                    created = True
                    count = 0
                    while block := response.read(1024 * 1024):
                        count += len(block)
                        if count > 512 * 1024**2:
                            raise ValueError("Helper download exceeds limit")
                        out.write(block)
            return
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.IncompleteRead,
        ) as error:
            if created:
                archive.unlink(missing_ok=True)
            retryable = not isinstance(error, urllib.error.HTTPError) or error.code in (
                429,
                500,
                502,
                503,
                504,
            )
            if not retryable or attempt == 2:
                raise
            pause(2**attempt)
        except BaseException:
            if created:
                archive.unlink(missing_ok=True)
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, default=Path("build-assets"))
    args = parser.parse_args()
    root = args.assets.resolve()
    manifest = json.loads((root / "components.json").read_text(encoding="utf-8"))
    pending = {}
    for item in manifest["components"]:
        name = item["name"]
        if (
            name not in {"tunnel-client", "ffmpeg", "ffprobe"}
            or item["binary"] != "bin/" + name + ".exe"
        ):
            raise ValueError("Unknown helper")
        url = urllib.parse.urlsplit(item["source_url"])
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Use a public HTTPS helper source")
        target = root / item["binary"]
        if target.exists():
            if target.is_symlink() or sha(target) != item["sha256"]:
                raise ValueError("Existing helper differs; preserved it")
            continue
        pending.setdefault(item["source_url"], []).append(item)
    with tempfile.TemporaryDirectory(prefix="udb-public-helpers-") as folder:
        for number, (url, items) in enumerate(pending.items()):
            archive = Path(folder) / (str(number) + ".zip")
            download(url, archive)
            with zipfile.ZipFile(archive) as package:
                for item in items:
                    matches = [
                        entry
                        for entry in package.infolist()
                        if Path(entry.filename).name == item["name"] + ".exe"
                    ]
                    if len(matches) != 1 or matches[0].file_size > 512 * 1024**2:
                        raise ValueError("Helper archive layout is ambiguous")
                    temporary = Path(folder) / (item["name"] + ".exe")
                    with (
                        package.open(matches[0]) as source,
                        temporary.open("xb") as out,
                    ):
                        shutil.copyfileobj(source, out, 1024 * 1024)
                    if sha(temporary) != item["sha256"]:
                        raise ValueError("Public helper checksum mismatch")
                    target = root / item["binary"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as out, temporary.open("rb") as source:
                        shutil.copyfileobj(source, out)
                    print("Verified public helper:", item["name"], flush=True)
    verified_assets(root)
    print("Public build helper inventory verified", flush=True)


if __name__ == "__main__":
    main()
