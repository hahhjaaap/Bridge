"""Retry public build downloads without relaxing checksums or overwriting files."""

import io
import runpy
import urllib.error
from pathlib import Path

import pytest

download = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "tools/fetch_build_assets.py")
)["download"]
URL = "https://public.example/helper.zip"


def test_transient_http_error_is_retried_with_a_bound(tmp_path):
    attempts, pauses = [], []

    def open_url(url, **kwargs):
        attempts.append(url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(url, 503, "temporary", {}, None)
        return io.BytesIO(b"complete")

    target = tmp_path / "download.zip"
    download(URL, target, opener=open_url, pause=pauses.append)
    assert target.read_bytes() == b"complete"
    assert len(attempts) == 2 and pauses == [1]


def test_partial_download_is_discarded_before_retry(tmp_path):
    attempts, pauses = [], []

    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise urllib.error.URLError("interrupted")
            return super().read(size)

    def open_url(url, **kwargs):
        attempts.append(url)
        return (
            Interrupted(b"partial") if len(attempts) == 1 else io.BytesIO(b"complete")
        )

    target = tmp_path / "download.zip"
    download(URL, target, opener=open_url, pause=pauses.append)
    assert target.read_bytes() == b"complete" and len(attempts) == 2
    assert pauses == [1]


def test_cdn_recovers_after_multiple_http_503_responses(tmp_path):
    attempts, pauses = [], []

    def open_url(url, **kwargs):
        attempts.append(url)
        if len(attempts) < 5:
            raise urllib.error.HTTPError(url, 503, "temporary", {}, None)
        return io.BytesIO(b"verified-by-caller")

    target = tmp_path / "download.zip"
    download(URL, target, opener=open_url, pause=pauses.append)
    assert target.read_bytes() == b"verified-by-caller"
    assert len(attempts) == 5 and pauses == [1, 2, 4, 8]


def test_permanent_http_error_is_not_retried(tmp_path):
    attempts, pauses = [], []

    def open_url(url, **kwargs):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 404, "missing", {}, None)

    target = tmp_path / "download.zip"
    with pytest.raises(urllib.error.HTTPError):
        download(URL, target, opener=open_url, pause=pauses.append)
    assert len(attempts) == 1 and not pauses and not target.exists()


def test_retry_limit_does_not_leave_a_partial_file(tmp_path):
    attempts, pauses = [], []

    def open_url(url, **kwargs):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 503, "temporary", {}, None)

    target = tmp_path / "download.zip"
    with pytest.raises(urllib.error.HTTPError):
        download(URL, target, opener=open_url, pause=pauses.append)
    assert len(attempts) == 6 and pauses == [1, 2, 4, 8, 16] and not target.exists()


def test_existing_download_is_preserved(tmp_path):
    target = tmp_path / "download.zip"
    target.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        download(
            URL,
            target,
            opener=lambda *a, **k: pytest.fail("Must not fetch over an existing file"),
        )
    assert target.read_bytes() == b"keep"
