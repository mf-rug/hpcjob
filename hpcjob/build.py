"""Where is this code actually running from?

`__version__` alone cannot answer the question that matters when the tool
misbehaves: **am I running the checkout, or a snapshot installed from it?**
An editable install and `pip install .` both report the same version string,
but only the first tracks a `git pull` — so a repo that has moved ahead and a
CLI that has not look identical from the outside.

Answering it from outside the tool is unreliable: the console script lives
outside the repo either way, so a path check gives false positives, and the
interpreter that owns the entry point is not necessarily the `pip` on `PATH`
(asking the wrong one prints nothing at all, which reads as "not installed"
rather than "wrong question"). The package knows both facts about itself for
free, so it reports them.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def source_root() -> Path:
    """Directory the package is imported from."""
    return Path(__file__).resolve().parent


def revision() -> str | None:
    """`<short-sha>` or `<short-sha>-dirty`, or None if not in a work tree."""
    root = source_root()
    if _git(["rev-parse", "--is-inside-work-tree"], root) != "true":
        return None
    sha = _git(["rev-parse", "--short", "HEAD"], root)
    if not sha:
        return None
    # Tracked modifications only. An untracked stray file in the checkout says
    # nothing about the code being run, and a marker that fires on it teaches
    # you to ignore the marker.
    dirty = _git(["status", "--porcelain", "--untracked-files=no"], root)
    return f"{sha}-dirty" if dirty else sha


def version_string(version: str) -> str:
    """Version annotated with where the code came from."""
    rev = revision()
    return f"{version} ({rev})" if rev else f"{version} (installed snapshot)"


def report(version: str) -> str:
    """Full `doctor` output: how this tool is installed and what it reads."""
    from .config import CONFIG_PATH

    root = source_root()
    rev = revision()
    lines = [
        f"hpcjob      {version_string(version)}",
        f"package     {root}",
        f"interpreter {sys.executable}",
        f"entry point {sys.argv[0]}",
    ]
    if rev:
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root) or "?"
        behind = _git(["rev-list", "--count", "HEAD..@{u}"], root)
        lines.append(f"checkout    {root.parent} (branch {branch})")
        if behind and behind != "0":
            lines.append(f"            !! {behind} commit(s) behind upstream — "
                         f"`git -C {root.parent} pull --ff-only`")
        if rev.endswith("-dirty"):
            lines.append("            (working tree has uncommitted changes)")
    else:
        lines.append("checkout    none — this is a snapshot install, so a "
                     "`git pull` in any")
        lines.append("            checkout will NOT change what this command "
                     "runs. Reinstall")
        lines.append("            with `pip install -e /path/to/hpcjob` to "
                     "track a checkout.")

    lines.append(f"registry    {CONFIG_PATH}"
                 f"{'' if CONFIG_PATH.exists() else '  (missing)'}")
    return "\n".join(lines)
