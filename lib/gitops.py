"""Thin wrappers around the ``git`` CLI via subprocess.

Provides a small, stdlib-only surface for staging files, committing, pushing,
and archiving PDF reports.  A secret-safety guard (assert_no_secrets_staged)
runs before every commit to catch accidental credential inclusion.

Public API
----------
GitError                  -- exception raised on git failures
run_git                   -- low-level runner; returns stdout as str
current_branch            -- name of HEAD branch
has_staged_changes        -- True if the index has staged content
stage                     -- git-add paths (defaults to DATA_DIR + REPORTS_DIR)
assert_no_secrets_staged  -- raise GitError if secrets detected in index
commit                    -- stage-safe commit; returns False on empty index
push                      -- push a branch to a remote
archive_report            -- copy latest.pdf to archive/report_<date>.pdf
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lib import config

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class GitError(Exception):
    """Raised when a git command exits with a non-zero status or when a
    secret-safety check fails."""


# ---------------------------------------------------------------------------
# Low-level runner
# ---------------------------------------------------------------------------


def run_git(*args: str, check: bool = True, cwd: Path = config.REPO_ROOT) -> str:
    """Run ``git <args>`` in *cwd* and return stripped stdout.

    Parameters
    ----------
    *args:
        Arguments forwarded to the git subprocess (e.g. ``"status"``, ``"-s"``).
    check:
        If *True* (default), raise :class:`GitError` when the process exits
        with a non-zero return code.
    cwd:
        Working directory for git; defaults to :data:`config.REPO_ROOT`.

    Returns
    -------
    str
        Captured stdout, stripped of leading/trailing whitespace.

    Raises
    ------
    GitError
        When *check* is *True* and the process exits non-zero.
    """
    cmd = ["git", *args]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",  # binary diffs (committed PDFs) must not crash the wrapper
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found; is git installed?") from exc

    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        raise GitError(
            f"git {' '.join(args)!r} failed (exit {result.returncode}): {stderr}"
        )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Read-only queries
# ---------------------------------------------------------------------------


def current_branch() -> str:
    """Return the name of the currently checked-out branch.

    Uses ``git symbolic-ref --short HEAD`` so it works even on a repo that has
    no commits yet.  Returns the raw branch name (e.g. ``"main"``).  On a
    detached HEAD falls back to ``git rev-parse --abbrev-ref HEAD`` which
    returns ``"HEAD"``.
    """
    # symbolic-ref works on empty repos (no commits) whereas rev-parse HEAD
    # fails with exit 128 when there is no commit yet.
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(config.REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    # Detached HEAD or other unusual state — fall back.
    return run_git("rev-parse", "--abbrev-ref", "HEAD")


def has_staged_changes() -> bool:
    """Return *True* if the index contains at least one staged file.

    Runs ``git diff --cached --name-only`` and checks whether the output is
    non-empty.
    """
    output = run_git("diff", "--cached", "--name-only")
    return bool(output)


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def stage(paths=None) -> None:
    """Stage *paths* for the next commit via ``git add -- <paths>``.

    Parameters
    ----------
    paths:
        An iterable of :class:`str` or :class:`~pathlib.Path` objects to add.
        Pass *None* (the default) to stage :data:`config.DATA_DIR` and
        :data:`config.REPORTS_DIR`.
    """
    if paths is None:
        targets = [config.DATA_DIR, config.REPORTS_DIR]
    else:
        targets = list(paths)

    str_targets = [str(p) for p in targets]
    run_git("add", "--", *str_targets)


# ---------------------------------------------------------------------------
# Secret-safety guard
# ---------------------------------------------------------------------------

_SENSITIVE_SUFFIXES = (".env", ".pem", ".key")
_PRIVATE_KEY_MARKER = "PRIVATE KEY"


def assert_no_secrets_staged() -> None:
    """Inspect the staged index for obvious secret material.

    Raises :class:`GitError` if any staged file path ends with ``.env``,
    ``.pem``, or ``.key``, **or** if the staged diff (``git diff --cached``)
    contains the substring ``"PRIVATE KEY"``.

    This is a defense-in-depth check and is called automatically by
    :func:`commit` before creating any commit object.
    """
    # Check staged file names.
    staged_names = run_git("diff", "--cached", "--name-only")
    for name in staged_names.splitlines():
        name_lower = name.lower()
        for suffix in _SENSITIVE_SUFFIXES:
            if name_lower.endswith(suffix):
                raise GitError(
                    f"Refusing to commit: staged file '{name}' has a sensitive "
                    f"extension ({suffix})."
                )

    # Check diff content for embedded private key material.
    diff_output = run_git("diff", "--cached")
    if _PRIVATE_KEY_MARKER in diff_output:
        raise GitError(
            "Refusing to commit: staged diff contains 'PRIVATE KEY' — "
            "a private key appears to be staged."
        )


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def commit(message: str) -> bool:
    """Create a commit with *message* if the index is non-empty.

    Calls :func:`assert_no_secrets_staged` before committing.

    Parameters
    ----------
    message:
        Commit message passed verbatim to ``git commit -m``.

    Returns
    -------
    bool
        *True* if a commit was created, *False* if the index was empty and the
        commit was skipped.

    Raises
    ------
    GitError
        On secret detection or git failure.
    """
    if not has_staged_changes():
        return False
    assert_no_secrets_staged()
    run_git("commit", "-m", message)
    return True


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def push(branch: str = "main", remote: str = "origin") -> None:
    """Push *branch* to *remote*.

    Parameters
    ----------
    branch:
        Local/remote branch name (default ``"main"``).
    remote:
        Git remote name (default ``"origin"``).

    Raises
    ------
    GitError
        If the push fails, including when the remote or tracking branch is not
        configured.  The error message includes actionable context.
    """
    try:
        run_git("push", remote, branch)
    except GitError as exc:
        msg = str(exc)
        if "does not appear to be a git repository" in msg or "remote" in msg.lower():
            raise GitError(
                f"Push to '{remote}/{branch}' failed — remote '{remote}' may not "
                f"be configured. Run: git remote add {remote} <url>\n"
                f"Original error: {msg}"
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Report archiving
# ---------------------------------------------------------------------------


def archive_report(
    latest_path: Path = config.LATEST_PDF_PATH,
    date_str: Optional[str] = None,
) -> Optional[Path]:
    """Copy *latest_path* to the archive directory as ``report_<date_str>.pdf``.

    Parameters
    ----------
    latest_path:
        Source PDF to archive; defaults to :data:`config.LATEST_PDF_PATH`.
    date_str:
        Date suffix for the archived filename in ``YYYY-MM-DD`` format.
        Defaults to today's UTC date.

    Returns
    -------
    Path or None
        The destination path if the copy succeeded, or *None* if *latest_path*
        does not exist.

    Notes
    -----
    The operation is idempotent: if the destination already exists it is
    overwritten.
    """
    if not Path(latest_path).exists():
        return None

    if date_str is None:
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.ARCHIVE_DIR / f"report_{date_str}.pdf"
    shutil.copy2(str(latest_path), str(dest))
    return dest


# ---------------------------------------------------------------------------
# Self-test (read-only)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"branch          : {current_branch()}")
    print(f"has_staged      : {has_staged_changes()}")
    assert_no_secrets_staged()
    print("gitops OK")
