"""Existing Companion limits and source-path policy constants."""

from udb.concurrency import MAX_CONCURRENT

MAX_SESSIONS = MAX_CONCURRENT
MAX_RETAINED_SESSIONS = 64
MAX_BUFFER_BYTES = 1024 * 1024
MAX_READ_BYTES = 128 * 1024
MAX_TERMINAL_OUTPUT_BYTES = 10 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SCAN_FILES = 20000
MAX_OUTPUT_CHARS = 14000
HIDDEN_DIRS = {
    ".git",
    ".udb-recovery",
    ".udb-work-state",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
}
HIDDEN_DIRS_CASEFOLD = {name.casefold() for name in HIDDEN_DIRS}
PROTECTED_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "connection.json",
    "runtime.json",
    "http-token",
    "computer-access.dpapi",
}
PROTECTED_SUFFIXES = {
    ".pem",
    ".p12",
    ".pfx",
    ".key",
    ".ckpt",
    ".safetensors",
    ".pt",
    ".pth",
}
PROTECTED_NAMES.update({"checkpoints", "datasets", "backups", "quarantine"})
PROTECTED_NAMES.update(
    {
        "checkpoint",
        "dataset",
        "training_data",
        "training-data",
        "weights",
        "models",
        ".ssh",
        ".aws",
        ".azure",
    }
)
PROTECTED_PREFIXES = (".env", "credentials", "secrets", "id_rsa", "id_ed25519")
