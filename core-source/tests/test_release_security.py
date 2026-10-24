"""Offline packaging security regressions (safe to run on Linux)."""
from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

from udb.release import PREFIX, ROOT_FILES, create_archive, verify_archive, write_build_info
from udb.release_policy import allowed_release_member, assert_safe_payload


@pytest.mark.parametrize("name", [
    "state/http-token",
    "state/tunnel/example.yaml",
    "state/connection.json",
    "state/computer-access.dpapi",
    "state/agent.sqlite3",
    "state/host-settings.json",
    "projects/demo/.udb-recovery/file",
    "projects/demo/.udb-work-state/file",
    "udb/__pycache__/core.pyc",
    "udb/file.pyc",
    "udb/file.bak",
    "udb/data.zip",
    "vendor/python-runtime/python311.zip",
    "vendor/state/secret.txt",
    "command/.udb-work-state",
    "udb/connection.json",
    "udb/HTTP-TOKEN",
    "vendor/unknown.exe",
    "vendor/python-runtime/Lib/__pycache__/unexpected.pyc",
    "tests/test_release_security.py",
    "THIRD_PARTY_NOTICES/example.dist-info/LICENSE.dll",
    "arbitrary/README.md",
    "../bridge.py",
    r"udb\bridge.py",
])
def test_release_path_denials(name):
    assert not allowed_release_member(name, ROOT_FILES)


@pytest.mark.parametrize("name", [
    "bridge.py",
    "udb/server.py",
    "udb/desktop_uia.ps1",
    "udb/build_info.json",
    "command/udb_cli.py",
    "vendor/tunnel-client.exe",
    "vendor/tunnel-client-source.json",
    "vendor/python-runtime/python.exe",
    "vendor/python-runtime/DLLs/_ssl.pyd",
    "vendor/python-runtime/Lib/encodings/__init__.py",
    "THIRD_PARTY_NOTICES/example.dist-info/LICENSE.txt",
    "THIRD_PARTY_NOTICES/example.dist-info/LICENSE.APACHE",
    "THIRD_PARTY_NOTICES/example.dist-info/LICENSE.BSD-3-Clause",
    "THIRD_PARTY_NOTICES/example.dist-info/LICENSE.LGPLv2.1",
])
def test_release_path_allowlist(name):
    assert allowed_release_member(name, ROOT_FILES)


@pytest.mark.parametrize("content", [
    b"ghp_" + b"A1" * 20,
    b"sk-proj-" + b"A1" * 20,
    b"AKIA" + b"ABCD1234" * 2,
    b"-----BEGIN PRIVATE KEY-----",
    b'{"http-token":"' + b"A1" * 20 + b'"}',
    b"Bearer " + b"A1" * 20,
])
def test_secret_signatures_rejected_without_echo(content):
    with pytest.raises(ValueError, match="credential") as error:
        assert_safe_payload(content)
    assert content.decode("ascii") not in str(error.value)


def test_nested_zip_bytes_rejected_even_if_extension_is_safe():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.txt", "nested data")
    with pytest.raises(ValueError, match="Nested ZIP"):
        assert_safe_payload(buffer.getvalue())


def test_synthetic_test_credentials_are_not_treated_as_real_keys():
    assert_safe_payload(b"sk-synthetic-NOT-A-REAL-KEY_123-")


def _write_synthetic_vendor(base):
    from udb.release import sha256

    payloads = {
        'vendor/python-runtime/python.exe': b'synthetic-python-runtime',
        'vendor/python-runtime/python3.dll': b'synthetic-python3-dll',
        'vendor/python-runtime/python311.dll': b'synthetic-python311-dll',
        'vendor/python-runtime/vcruntime140.dll': b'synthetic-vcruntime140',
        'vendor/python-runtime/vcruntime140_1.dll': b'synthetic-vcruntime140_1',
        'vendor/python-runtime/LICENSE.txt': b'synthetic-python-license',
        'vendor/python-runtime/python311._pth': b'Lib\n.\nDLLs\n',
        'vendor/python-runtime/DLLs/_ssl.pyd': b'synthetic-ssl-extension',
        'vendor/python-runtime/Lib/encodings/__init__.py': b'# synthetic encodings\n',
        'vendor/python-runtime/Lib/os.py': b'# synthetic os module\n',
        'vendor/tunnel-client.exe': b'synthetic-tunnel-client',
        'vendor/tunnel-LICENSES.txt': b'synthetic-tunnel-license-notice',
    }
    tunnel_hash = sha256(payloads['vendor/tunnel-client.exe'])
    license_hash = sha256(payloads['vendor/tunnel-LICENSES.txt'])
    source = {
        'repository': 'openai/tunnel-client',
        'tag_name': 'v0.0.18',
        'asset_name': 'tunnel-client-v0.0.18-windows-amd64.zip',
        'asset_sha256': 'sha256:' + 'a' * 64,
        'license_asset_name': 'tunnel-client-v0.0.18-windows-amd64-licenses.txt',
        'license_asset_sha256': 'sha256:' + 'b' * 64,
        'tunnel_executable_sha256': tunnel_hash,
        'license_file_sha256': license_hash,
    }
    payloads['vendor/tunnel-client-source.json'] = json.dumps(source, sort_keys=True).encode('utf-8')
    for name, content in payloads.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    records = {name: {'sha256': sha256(content), 'bytes': len(content)}
               for name, content in payloads.items()}
    (base / 'vendor' / 'DEPENDENCIES.json').write_text(json.dumps({
        'python_version': 'Python 3.11.9',
        'tunnel_client_release': 'v0.0.18',
        'origin': 'synthetic test fixture',
        'files': records,
    }, sort_keys=True), encoding='utf-8')


@pytest.fixture()
def package_fixture(tmp_path):
    from udb.release import WORKERS

    base = tmp_path / "source"
    (base / "udb").mkdir(parents=True)
    (base / "tests").mkdir()
    (base / "tests" / "test_release_security.py").write_text(
        'fake = b"ghp_" + b"A1" * 20\\n', encoding="utf-8"
    )
    for name in ROOT_FILES:
        (base / name).write_text("fixture\n", encoding="utf-8")
    for name in WORKERS:
        (base / "udb" / name).write_text("# worker\n", encoding="utf-8")
    (base / "udb" / "__init__.py").write_text("# module\n", encoding="utf-8")
    _write_synthetic_vendor(base)
    exe = tmp_path / "candidate.exe"
    exe.write_bytes(b"synthetic-not-executable")
    return base, exe, tmp_path / "candidate.zip"


def _pack(package_fixture):
    base, exe, output = package_fixture
    info = write_build_info(base, "synthetic")
    create_archive(base, exe, output, info)
    return output


def _rewrite_archive(path, replacement_name, replacement):
    with zipfile.ZipFile(path) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest_name = PREFIX + "RELEASE-MANIFEST.json"
    manifest = json.loads(contents[manifest_name])
    from udb.release import sha256
    manifest["files"][replacement_name[len(PREFIX):]] = {
        "sha256": sha256(replacement), "size_bytes": len(replacement),
    }
    contents[replacement_name] = replacement
    contents[manifest_name] = json.dumps(manifest).encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in contents.items():
            archive.writestr(name, value)


def test_manifest_cannot_override_path_allowlist(package_fixture):
    output = _pack(package_fixture)
    _rewrite_archive(output, PREFIX + "state/connection.json", b"bad")
    with pytest.raises(ValueError, match="Unsafe"):
        verify_archive(output)


def test_rehashed_manifest_cannot_hide_secret(package_fixture):
    output = _pack(package_fixture)
    fake_secret = b"ghp_" + b"A1" * 20
    _rewrite_archive(output, PREFIX + "bridge.py", fake_secret)
    with pytest.raises(ValueError, match="credential") as error:
        verify_archive(output)
    assert fake_secret.decode() not in str(error.value)


def test_rehashed_manifest_cannot_hide_nested_zip(package_fixture):
    output = _pack(package_fixture)
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("data", b"sample")
    _rewrite_archive(output, PREFIX + "bridge.py", nested.getvalue())
    with pytest.raises(ValueError, match="Nested ZIP"):
        verify_archive(output)


def test_vendor_runtime_allowlisted_and_private_vendor_fails(package_fixture):
    base, _, output = package_fixture
    stdlib = base / "vendor" / "python-runtime" / "Lib" / "encodings"
    assert (stdlib / "__init__.py").is_file()
    _pack(package_fixture)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert PREFIX + "vendor/python-runtime/python.exe" in names
        assert PREFIX + "plugin.json" not in names
        assert PREFIX + "mcp.json" not in names
        assert not any(name.startswith(PREFIX + "tests/") for name in names)
    assert verify_archive(output)["verified"]

    (base / "vendor" / "state").mkdir()
    (base / "vendor" / "state" / "connection.json").write_text('{"token":"secret"}')
    with pytest.raises(ValueError, match="Disallowed vendor"):
        create_archive(base, package_fixture[1], base.parent / "second.zip",
                       write_build_info(base, "synthetic"))


def test_tunnel_source_manifest_requires_matching_binary_and_license(package_fixture):
    from udb.release import validate_vendor_payload, sha256

    base, _, _ = package_fixture
    source_path = base / "vendor" / "tunnel-client-source.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["tunnel_executable_sha256"] = "0" * 64
    source_path.write_text(json.dumps(source), encoding="utf-8")
    payloads = {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in (base / "vendor").rglob("*") if path.is_file()
    }
    records = json.loads(payloads["vendor/DEPENDENCIES.json"])
    records["files"]["vendor/tunnel-client-source.json"] = {
        "sha256": sha256(payloads["vendor/tunnel-client-source.json"]),
        "bytes": len(payloads["vendor/tunnel-client-source.json"]),
    }
    payloads["vendor/DEPENDENCIES.json"] = json.dumps(records, indent=2).encode("utf-8")
    with pytest.raises(ValueError, match="Tunnel executable source hash mismatch"):
        validate_vendor_payload(payloads)


def test_prepare_vendor_stages_unpacked_stdlib(tmp_path):
    source_file = Path(__file__).resolve().parents[1] / "Prepare-Vendor.py"
    spec = importlib.util.spec_from_file_location("udb_prepare_vendor_test", source_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / "python"
    (home / "Lib" / "encodings").mkdir(parents=True)
    (home / "Lib" / "encodings" / "__init__.py").write_text("demo\n")
    (home / "Lib" / "tests").mkdir()
    (home / "Lib" / "tests" / "bad.py").write_text("ignored\n")
    runtime = tmp_path / "runtime"
    module.stage_stdlib(home, runtime)
    assert (runtime / "Lib" / "encodings" / "__init__.py").read_text() == "demo\n"
    assert not (runtime / "Lib" / "tests").exists()
    assert not list(runtime.rglob("*.zip"))


def test_gitignore_denies_machine_state():
    path = Path(__file__).resolve().parents[1] / ".gitignore"
    patterns = path.read_text(encoding="utf-8").splitlines()
    required = ["state/", ".udb-recovery/", ".udb-work-state/", "*.bak",
                "__pycache__/", "*.pyc", "*.zip"]
    assert all(item in patterns for item in required)


def test_verify_package_cli_fails_without_leaking_secret(package_fixture):
    import subprocess
    import sys

    output = _pack(package_fixture)
    fake_secret = b"ghp_" + b"A1" * 20
    _rewrite_archive(output, PREFIX + "bridge.py", fake_secret)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "Verify-Package.py"), str(output)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "credential" in result.stderr
    assert fake_secret.decode("ascii") not in result.stdout + result.stderr
