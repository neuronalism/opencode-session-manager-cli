"""Replication of OpenCode's ``Project.resolve`` identity algorithm.

Ported from ``opencode-2.2.5/packages/core/src/project.ts`` (``Project.resolve``,
``gitDiscover``, ``hgDiscover``). ocsm must derive the exact same project id
OpenCode would, otherwise imported/moved sessions become invisible to the
OpenCode session list (it filters ``session_v2.project_id`` by the live
resolution of the current working directory).

Identity rules, in priority order for git repositories:

1. ``origin`` remote URL  -> ``sha1("git-remote:" + host/pathname)`` where the
   host is lower-cased, a leading ``/`` and trailing ``.git``/``/`` are
   stripped from the path (``project.ts:299-328``).
2. Cached id: the trimmed content of the ``opencode`` file inside the repo's
   git *common* directory, when present (``project.ts:291-297``).
3. First root commit (sorted ``git rev-list --max-parents=0 HEAD``)
   (``project.ts:330-333``).
4. ``"global"`` literal when none of the above exist.

Mercurial repositories use the cached id or the first root changeset.
Plain directories get ``sha1("directory:" + realpath)`` (``project.ts:435-440``).

One Windows subtlety proven against a real database: the ``directory:`` hash
input uses the **native-separator realpath** (``C:\\Users\\WH`` →
``afaaaff98f5c837b24ee09a3e6e6b48a649ad9e2``), while ``project.worktree`` and
``session_v2.directory`` columns store the **forward-slash** form
(``database/path.ts`` ``storagePath``). This module therefore works in native
form internally and converts only at the storage boundary via
:func:`to_storage_path`.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

PROJECT_GLOBAL = "global"

_SCP_LIKE = re.compile(r"^([^@/:]+@)?([^/:]+):(.+)$")


@dataclass(frozen=True)
class ResolvedProject:
    """Outcome of :func:`resolve_project` — mirrors ``Project.Resolved``.

    ``directory``/``canonical``/``vcs_store`` use native OS separators; convert
    with :func:`to_storage_path` before writing any database column.
    """

    id: str
    directory: str
    canonical: str
    vcs_type: str | None = None  # "git" | "hg" | None
    vcs_store: str | None = None


def sha1_hex(text: str) -> str:
    """``Hash.fast`` = plain sha1 hex digest (``util/hash.ts:4-6``)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def to_storage_path(path: str | Path) -> str:
    """``storagePath``: database columns always store forward slashes."""
    return os.fspath(path).replace("\\", "/")


def _normalize_remote(value: str) -> str | None:
    """``url()``/``parts()`` from project.ts:307-328.

    Parses remote URLs the same way Node's ``URL.parse`` does: real URLs use
    hostname/pathname (``file:`` remotes yield no identity), anything else
    falls back to scp-like ``user@host:path`` matching. Empty host or path
    means no identity.
    """
    text = value.strip()
    if not text:
        return None

    try:
        split = urllib.parse.urlsplit(text)
    except ValueError:
        split = None
    if split is not None and split.scheme:
        # URL branch mirrors URL.parse: "file:" remotes carry no identity and
        # scheme-only hosts ("D:/x") parse with an empty hostname → no identity.
        if split.scheme.lower() == "file":
            return None
        try:
            host = split.hostname or ""
        except ValueError:
            host = ""
        return _parts(host, split.path or "")

    scp = _SCP_LIKE.match(text)
    if scp:
        return _parts(scp.group(2), scp.group(3))
    return None


def _parts(host: str, name: str) -> str | None:
    pathname = re.sub(r"^/+", "", name)
    pathname = re.sub(r"\.git/?$", "", pathname)
    pathname = re.sub(r"/+$", "", pathname)
    if not host or not pathname:
        return None
    return f"{host.lower()}/{pathname}"


def _run_git(worktree: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=worktree,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _run_hg(worktree: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["hg", *args],
            cwd=worktree,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            env={**os.environ, "HGPLAIN": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _cached_id(store_dir: str) -> str | None:
    """Id cached in ``<store>/opencode`` (project.ts:291-297)."""
    try:
        text = Path(store_dir, "opencode").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _native(path: str | None) -> str | None:
    """git prints forward-slash paths; Node's ``path.resolve`` normalizes them
    to native separators (git.ts:227) — mirror that."""
    return os.path.abspath(path) if path else None


def _git_identity(worktree: str) -> ResolvedProject:
    # repo.discover: toplevel / git dir / common dir (git.ts:217-267).
    top = _native(_run_git(worktree, "rev-parse", "--show-toplevel")) or worktree
    git_dir = _native(_run_git(worktree, "rev-parse", "--absolute-git-dir"))
    common_dir = _native(_run_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))

    project_id: str | None = None
    origin = _run_git(worktree, "remote", "get-url", "origin")  # exit 2: no such remote
    if origin:
        normalized = _normalize_remote(origin)
        if normalized:
            project_id = sha1_hex(f"git-remote:{normalized}")
    if project_id is None and common_dir:
        project_id = _cached_id(common_dir)
    if project_id is None:
        roots = _run_git(worktree, "rev-list", "--max-parents=0", "HEAD")
        if roots:
            shas = sorted(line.strip() for line in roots.splitlines() if line.strip())
            if shas:
                project_id = shas[0]
    if project_id is None:
        project_id = PROJECT_GLOBAL

    # canonical: the main checkout when resolving from a linked worktree
    # (project.ts:350-356).
    canonical = top
    if git_dir and common_dir and not _same_path(git_dir, common_dir):
        canonical = _main_worktree(worktree) or top

    return ResolvedProject(
        id=project_id,
        directory=top,
        canonical=canonical,
        vcs_type="git",
        vcs_store=common_dir or git_dir,
    )


def _main_worktree(worktree: str) -> str | None:
    out = _run_git(worktree, "worktree", "list", "--porcelain")
    if not out:
        return None
    # Porcelain output lists the main worktree first; each block starts with
    # ``worktree <path>``. Linked worktrees follow; ``bare`` marks bare mains.
    for line in out.splitlines():
        if line.startswith("worktree "):
            return _native(line[len("worktree "):].strip())
    return None


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _hg_identity(dot_hg: str) -> ResolvedProject:
    worktree = os.path.dirname(dot_hg)
    project_id = _cached_id(dot_hg)
    if project_id is None:
        out = _run_hg(worktree, "log", "-r", "roots(all())", "-T", "{node}\n")
        if out:
            nodes = sorted(line.strip() for line in out.splitlines() if line.strip())
            if nodes:
                project_id = nodes[0]
    return ResolvedProject(
        id=project_id or PROJECT_GLOBAL,
        directory=worktree,
        canonical=worktree,
        vcs_type="hg",
        vcs_store=dot_hg,
    )


def _find_marker(directory: str) -> str | None:
    """``fs.up({ targets: [".git", ".hg"], mode: "first" })``: nearest ancestor
    (starting at ``directory`` itself) holding either marker."""
    current = directory
    while True:
        for marker in (".git", ".hg"):
            candidate = os.path.join(current, marker)
            if os.path.exists(candidate):
                return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def resolve_project(directory: str | Path) -> ResolvedProject:
    """Resolve the project identity for ``directory`` like ``Project.resolve``.

    The input is normalized through ``realpath`` first (``fs.resolve``), so
    symlinks and relative inputs are handled the same way OpenCode handles
    them. The result's paths keep native separators; see :func:`to_storage_path`.
    """
    native = os.path.realpath(os.path.abspath(os.fspath(directory)))
    marker = _find_marker(native)
    if marker is not None and os.path.basename(marker) == ".git":
        return _git_identity(os.path.dirname(marker))
    if marker is not None and os.path.basename(marker) == ".hg":
        return _hg_identity(marker)
    return ResolvedProject(
        id=sha1_hex(f"directory:{native}"),
        directory=native,
        canonical=native,
        vcs_type=None,
        vcs_store=None,
    )


def relative_subpath(project_directory: str, location: str) -> str:
    """``subpath``: location relative to the resolved worktree root, forward
    slashes, ``"."`` at the root itself (session.ts:308, transfer.ts:80-82)."""
    rel = os.path.relpath(os.path.abspath(location), os.path.abspath(project_directory))
    return to_storage_path(rel)
