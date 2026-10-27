"""Shared policy error and stable digests for the core and owner-only services.

No owner permissions or grants are created here.
"""

from __future__ import annotations

import hashlib
import json


class PolicyError(ValueError):
    """An operation violated a locally enforced project/owner policy."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
