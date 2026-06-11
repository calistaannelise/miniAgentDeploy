"""Resolve a writable root for runtime state.

BioAPEX is file-first: sessions, the memory index, and artifacts all persist
to disk under the backend project root. That assumes a *writable* filesystem,
which is true for local dev and any long-running host with a persistent disk.

On read-only serverless deploys (e.g. Vercel functions, where the bundle at
``/var/task`` is read-only and only ``/tmp`` is writable) those writes fail.
``resolve_data_dir`` returns the project root when it is writable, and a
writable scratch directory otherwise.

IMPORTANT: the scratch dir on serverless platforms is *ephemeral* and *not*
shared across instances — state written there does not reliably survive across
requests or cold starts. This is a stopgap to let the app boot and serve a
request, not a substitute for a persistent disk. See ``DEPLOYMENT_NOTES.md``.

Read-only *resources* (skills/, workspace/, knowledge/, memory/ content,
SKILLS_SNAPSHOT.md, config.json) must still be read from the project root —
only pure-output trees (sessions/, storage/, artifacts/) get redirected here.
"""

from __future__ import annotations

import os
from pathlib import Path

_DATA_DIR_ENV_VAR = "BIOAPEX_DATA_DIR"
_DEFAULT_SCRATCH_DIR = Path("/tmp/bioapex")

# Cache the resolution per project root: the writability of a deployment's
# filesystem does not change within a process, and the probe touches disk.
_RESOLVED: dict[str, Path] = {}


def _is_writable(path: Path) -> bool:
    probe = path / ".bioapex_write_probe"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_data_dir(base_dir: Path | str) -> Path:
    """Return a writable root for runtime state derived from *base_dir*.

    - ``BIOAPEX_DATA_DIR`` (if set) wins — explicit operator override.
    - Otherwise, return *base_dir* when it is writable (local dev, persistent
      hosts).
    - Otherwise, fall back to a writable scratch dir (read-only serverless).

    The returned directory is guaranteed to exist.
    """
    base = Path(base_dir)
    cache_key = str(base)
    cached = _RESOLVED.get(cache_key)
    if cached is not None:
        return cached

    override = os.getenv(_DATA_DIR_ENV_VAR, "").strip()
    if override:
        data_dir = Path(override).expanduser()
    elif _is_writable(base):
        data_dir = base
    else:
        data_dir = _DEFAULT_SCRATCH_DIR

    data_dir.mkdir(parents=True, exist_ok=True)
    _RESOLVED[cache_key] = data_dir
    return data_dir
