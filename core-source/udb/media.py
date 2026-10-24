"""Owner-approved, allowlisted HTTPS HLS -> MP4 on the Windows host.

This is intentionally NOT a host shell or arbitrary network downloader.
Only locally approved requests may call run_download().
"""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

ALLOWED_FILE = "media-allowed-hosts.json"
DEFAULT_HOSTS = ("ynvideo.kshunze.com",)
MAX_BYTES = 2 * 1024**3
MAX_SECONDS = 7200
MAX_MANIFEST = 1024 * 1024
FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.mp4$")

def _hosts(agent):
    file = agent.state / ALLOWED_FILE
    if not file.exists():
        file.write_text(json.dumps({"allowed_hosts": list(DEFAULT_HOSTS)}, indent=2) + "\n", encoding="utf8")
    data = json.loads(file.read_text(encoding="utf8"))
    hosts = data.get("allowed_hosts")
    if not isinstance(hosts, list) or len(hosts) > 25:
        raise ValueError("Invalid local media host allowlist")
    valid = set()
    for host in hosts:
        if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,252}", host):
            raise ValueError("Invalid allowlisted hostname")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("IP literals are not allowed in media allowlist")
        valid.add(host.lower())
    return valid

def validate(agent, url, filename):
    if not isinstance(url, str) or not 15 <= len(url) <= 2048 or any(ord(c) < 32 for c in url):
        raise ValueError("Invalid HTTPS HLS URL")
    if not isinstance(filename, str) or not FILENAME.fullmatch(filename):
        raise ValueError("Output file must be a simple ASCII .mp4 name")
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment or p.port not in (None, 443):
        raise ValueError("Only approved HTTPS HLS addresses are allowed")
    host = p.hostname.lower()
    if host not in _hosts(agent):
        raise ValueError("Video host is not on the owner's local allowlist")
    if not p.path.lower().endswith(".m3u8"):
        raise ValueError("Input must be an HLS .m3u8 playlist")
    downloads = Path.home() / "Downloads"
    if not downloads.is_dir() or downloads.is_symlink():
        raise ValueError("Downloads directory is missing or not a plain folder")
    output = downloads / filename
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output file already exists; choose a different name")
    return downloads, output, host

def _manifest(url, host):
    class SameHostRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            p = urllib.parse.urlsplit(newurl)
            if p.scheme != "https" or p.hostname != host or p.port not in (None, 443) or p.username or p.password:
                raise ValueError("HLS redirect outside approved host")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    opener = urllib.request.build_opener(SameHostRedirect)
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "UniversalDesktopBridge/0.3.2"}), timeout=25) as r:
        content = r.read(MAX_MANIFEST + 1)
    if len(content) > MAX_MANIFEST:
        raise ValueError("HLS playlist is too large")
    text = content.decode("utf8", "strict")
    if not text.lstrip().startswith("#EXTM3U") or "#EXT-X-ENDLIST" not in text:
        raise ValueError("Only complete VOD HLS playlists are supported")
    if any(item in text for item in ("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:", "#EXT-X-STREAM-INF:", "#EXT-X-MAP:", "#EXT-X-BYTERANGE:")):
        raise ValueError("Encrypted or multivariant HLS is not supported by this restricted tool")
    duration = 0.0
    segments = 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration += float(line.split(":", 1)[1].split(",", 1)[0])
        if not line or line.startswith("#"):
            continue
        target = urllib.parse.urlsplit(urllib.parse.urljoin(url, line))
        if target.scheme != "https" or target.hostname != host or target.port not in (None, 443):
            raise ValueError("HLS media segment outside approved host")
        if target.username or target.password or target.fragment:
            raise ValueError("Unsafe HLS segment URL")
        segments += 1
    if segments < 1 or not (0 < duration <= MAX_SECONDS):
        raise ValueError("Empty or overly long HLS playlist")
    return duration, segments

def propose(agent, url, filename):
    downloads, output, host = validate(agent, url, filename)
    return agent.propose("media", {
        "project": "Media", "root": str(downloads.resolve()), "url": url,
        "filename": filename, "host": host, "task": "Download HLS to Downloads using FFmpeg",
    })

def run_download(agent, rid, payload):
    from .resources import guard_new_heavy_task
    url, filename = payload["url"], payload["filename"]
    downloads, output, host = validate(agent, url, filename)
    if payload["root"] != str(downloads.resolve()) or payload["host"] != host:
        raise ValueError("Media request scope changed after local review")
    guard_new_heavy_task(agent, 256)
    duration, segments = _manifest(url, host)
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg/FFprobe missing from Windows PATH")
    temp = downloads / (".udb-" + rid + ".mp4")
    if temp.exists():
        raise RuntimeError("Unexpected existing temporary video")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-protocol_whitelist", "http,https,tcp,tls,crypto",
        "-i", url, "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart",
        "-fs", str(MAX_BYTES), str(temp),
    ]
    try:
        process = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=3600, check=False)
        if process.returncode != 0:
            raise RuntimeError("FFmpeg could not complete this approved HLS download")
        probe = subprocess.run([
            ffprobe, "-v", "error", "-show_entries", "format=duration,size:stream=codec_type",
            "-of", "json", str(temp),
        ], stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=45)
        result = json.loads(probe.stdout)
        produced = float(result["format"]["duration"])
        nbytes = int(result["format"]["size"])
        if produced < duration * .98 or not (0 < nbytes <= MAX_BYTES):
            raise ValueError("Output does not match source duration or size bounds")
        if not any(s.get("codec_type") == "video" for s in result.get("streams", [])):
            raise ValueError("Output has no video stream")
        if output.exists() or output.is_symlink():
            raise FileExistsError("Output was created by another process")
        # Hard-link is an atomic, no-replace publish on the same NTFS volume.
        os.link(temp, output)
        temp.unlink()
        agent.audit("media_download", {"file": filename, "host": host, "size": nbytes})
        sha = hashlib.sha256()
        with output.open('rb') as source:
            for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
                sha.update(chunk)
        return {"file": str(output), "bytes": nbytes, "duration_seconds": round(produced, 2),
                "segments": segments, "sha256": sha.hexdigest()}
    finally:
        temp.unlink(missing_ok=True)
