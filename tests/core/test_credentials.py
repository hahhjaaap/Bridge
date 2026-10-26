import pytest

from udb.credentials import load_key, normalize_key, save_key


class FakeCred:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def CredWrite(self, record, flags):
        assert isinstance(record["CredentialBlob"], str)
        self.record = record

    def CredRead(self, target, kind):
        assert target == self.record["TargetName"]
        return self.record


def test_unicode_pywin32_contract_and_roundtrip():
    backend = FakeCred()
    fake = "sk-synthetic-123_ABC-" * 20
    save_key(" \r\n" + fake + "\r\n", backend)
    assert load_key(backend) == fake
    assert isinstance(backend.record["CredentialBlob"], str)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "sk-test\nmore",
        "sk-test\tmore",
        "sk-test\0",
        "sk-tést",
        "sk-test\u200b",
        "x" * 1281,
    ],
)
def test_reject_bad_paste_without_echoing_secret(value):
    with pytest.raises(ValueError) as e:
        normalize_key(value)
    if value.strip():
        assert value not in str(e.value)


def test_legacy_utf16_read():
    backend = FakeCred()
    backend.record = {
        "TargetName": "UniversalDesktopBridge/Tunnel",
        "CredentialBlob": "sk-synthetic".encode("utf-16-le"),
    }
    assert load_key(backend) == "sk-synthetic"
