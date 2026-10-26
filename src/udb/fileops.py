"""Bounded, scoped source operations using the existing Agent path policy."""

from __future__ import annotations

import fnmatch
import time

from .core import SOURCE_EXT, PolicyError


def read_files(agent, project, paths):
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 20
        or any(not isinstance(p, str) for p in paths)
    ):
        raise PolicyError("Provide 1..20 source paths")
    rows = []
    size = 0
    for path in paths:
        row = agent.read(project, path)
        size += len(row["text"].encode("utf8"))
        if size > 512 * 1024:
            raise PolicyError("Combined source response exceeds 512 KiB")
        rows.append(row)
    return {"files": rows, "bytes": size}


def search_files(agent, project, path=".", pattern="*", text="", limit=100):
    if (
        not isinstance(pattern, str)
        or not 1 <= len(pattern) <= 120
        or not isinstance(text, str)
        or len(text) > 200
    ):
        raise PolicyError("Invalid filename pattern or literal search text")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise PolicyError("Result limit must be 1..200")
    start = agent.path(project, path)
    _, root = agent.project(project)
    if not start.is_dir():
        raise PolicyError("Search root must be a project directory")
    pending = [start]
    matches = []
    scanned = 0
    byte_count = 0
    truncated = False
    deadline = time.monotonic() + 3
    while pending and scanned < 10000 and time.monotonic() < deadline:
        folder = pending.pop()
        try:
            agent.path(project, folder.relative_to(root).as_posix())
            with __import__("os").scandir(folder) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > 10000 or time.monotonic() >= deadline:
                        truncated = True
                        break
                    rel = (
                        __import__("pathlib")
                        .Path(entry.path)
                        .relative_to(root)
                        .as_posix()
                    )
                    try:
                        target = agent.path(project, rel)
                        if target.is_dir():
                            pending.append(target)
                            continue
                        if (
                            target.suffix.lower() not in SOURCE_EXT
                            or not fnmatch.fnmatch(
                                target.name.casefold(), pattern.casefold()
                            )
                        ):
                            continue
                        row = {"path": rel}
                        if text:
                            data = agent.read_bytes(project, rel)
                            byte_count += len(data)
                            if byte_count > 4 * 1024 * 1024:
                                truncated = True
                                pending.clear()
                                break
                            lines = data.decode("utf-8-sig").splitlines()
                            hits = [
                                {"line": i + 1, "text": line[:300]}
                                for i, line in enumerate(lines)
                                if text.casefold() in line.casefold()
                            ][:10]
                            if not hits:
                                continue
                            row["matches"] = hits
                        matches.append(row)
                        if len(matches) >= limit:
                            truncated = True
                            pending.clear()
                            break
                    except (PolicyError, OSError, UnicodeError):
                        continue
        except (PolicyError, OSError):
            continue
    return {
        "items": matches,
        "scanned": min(scanned, 10000),
        "truncated": truncated or bool(pending),
        "protected_items_hidden": True,
        "literal_search": True,
    }


def propose_text_edit(
    agent, project, path, old_text, new_text, expected_sha256, occurrences=1
):
    if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
        raise PolicyError("Nonempty old_text and string new_text required")
    if type(occurrences) is not int or not 1 <= occurrences <= 100:
        raise PolicyError("Occurrences must be 1..100")
    row = agent.read(project, path)
    if row["sha256"] != expected_sha256:
        raise PolicyError("File changed since it was read")
    if row["text"].count(old_text) != occurrences:
        raise PolicyError("Exact occurrence count mismatch")
    return agent.propose_write(
        project, path, row["text"].replace(old_text, new_text), expected_sha256
    )
