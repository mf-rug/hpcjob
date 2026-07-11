"""Back-compat entry points.

`hpc-submit ...` and `rsyncer ...` keep their original CLIs but forward to the
merged `hpcjob` dispatcher, so existing habits, scripts, and docs keep working.
Neither shim knows about --cluster; both use the registry's default cluster.
"""

from __future__ import annotations

import sys

from .cli import main as hpcjob_main


def _run(translated_argv: list[str]) -> None:
    sys.argv = ["hpcjob"] + translated_argv
    hpcjob_main()


def hpc_submit_main() -> None:
    """Translate the legacy `hpc-submit` CLI into `hpcjob` subcommands."""
    args = sys.argv[1:]

    if "--status" in args:
        i = args.index("--status")
        _run(["status", args[i + 1]])
        return
    if "--cancel" in args:
        i = args.index("--cancel")
        ids = []
        for a in args[i + 1:]:
            if a.startswith("-"):
                break
            ids.append(a)
        _run(["cancel", *ids])
        return
    if "--check" in args:
        _run(["check"])
        return
    if "--init" in args:
        # Legacy --init set up a single cluster; new tool migrates the registry.
        _run(["init", "--migrate"])
        return

    # Otherwise a submit: legacy flags (--files/--jobname/--overwrite) match
    # `hpcjob submit` verbatim, so pass them straight through.
    _run(["submit", *args])


def rsyncer_main() -> None:
    """Translate the legacy `rsyncer` CLI into `hpcjob` subcommands."""
    args = sys.argv[1:]

    if "--recent" in args:
        # Drop the flag; remaining tokens (N, --days D, --all, -y) map 1:1.
        rest = [a for a in args if a != "--recent"]
        _run(["recent", *rest])
        return

    # Default: a pull. Legacy flags (--filter, -y/--yes, --dest, --path) match
    # `hpcjob pull` verbatim.
    _run(["pull", *args])
