"""hpcjob command-line entry point.

Subcommands:
  submit    upload a job directory and sbatch it        (was: hpc-submit)
  pull      find a remote dir by name and rsync it down (was: rsyncer)
  recent    list recently active remote job directories (was: rsyncer --recent)
  status    query a job's state
  cancel    scancel one or more jobs
  clusters  list configured clusters
  doctor    how this tool is installed, and where it reads config from
  check     test SSH + remote path for a cluster
  init      create / migrate the cluster registry

Every subcommand that talks to a cluster accepts --cluster/-c NAME; without it,
the registry's default_cluster is used.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import interactive_init, list_clusters, load_registry, resolve_cluster
from .ssh import test_remote_path, test_ssh_connection


def _add_cluster_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("-c", "--cluster", default=None, metavar="NAME",
                   help="Cluster from the registry (default: registry's default_cluster).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hpcjob",
        description="Submit SLURM jobs to and pull results from remote HPC clusters via SSH.",
    )
    from .build import version_string
    parser.add_argument("--version", action="version",
                        version=f"hpcjob {version_string(__version__)}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # submit
    p = sub.add_parser("submit", help="Upload a job directory and submit with sbatch.")
    p.add_argument("job_script", type=Path, help="Path to the .sh job script.")
    p.add_argument("--files", nargs="+", type=Path, default=[], metavar="PATH",
                   help="Additional files/dirs to transfer alongside the job dir.")
    p.add_argument("--jobname", default=None,
                   help="Override remote dir name (default: #SBATCH --job-name).")
    p.add_argument("--overwrite", action="store_true",
                   help="Overwrite the remote directory if it exists (no prompt).")
    p.add_argument("--jobs-dir", default=None, metavar="PATH", dest="jobs_dir",
                   help="Remote base dir for this job, overriding the cluster's "
                        "configured jobs_dir (e.g. .../rfd3_jobs).")
    _add_cluster_flag(p)

    # pull
    p = sub.add_parser("pull", help="Find a remote dir by name (or abs path) and rsync it down.")
    p.add_argument("target", help="Folder name to search for, or an absolute remote path.")
    p.add_argument("--filter", action="store_true", dest="filter_exts",
                   help="List remote file extensions and choose which to sync.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Non-interactive: skip confirmations, auto-select first match.")
    p.add_argument("--dest", default=None, metavar="DIR",
                   help="Sync into DIR instead of ./<name>/.")
    p.add_argument("--path", default=None, metavar="PREFIX", dest="path_prefix",
                   help="Restrict multi-match results to paths starting with PREFIX.")
    _add_cluster_flag(p)

    # recent
    p = sub.add_parser("recent", help="List recently active remote job directories.")
    p.add_argument("n", nargs="?", type=int, default=20, help="How many to list (default 20).")
    p.add_argument("--days", type=int, default=29, help="SLURM look-back window (default 29).")
    p.add_argument("--all", action="store_true", dest="all_paths",
                   help="Skip SLURM; scan all configured search paths (slow).")
    p.add_argument("-y", "--yes", action="store_true", help="Non-interactive.")
    _add_cluster_flag(p)

    # status
    p = sub.add_parser("status", help="Query a job's state (squeue, then sacct).")
    p.add_argument("job_id", type=int)
    _add_cluster_flag(p)

    # cancel
    p = sub.add_parser("cancel", help="Cancel one or more jobs with scancel.")
    p.add_argument("job_id", type=int, nargs="+")
    _add_cluster_flag(p)

    # preflight
    p = sub.add_parser("preflight",
                       help="Is the cluster up, am I in budget, which GPUs are free?")
    p.add_argument("--all", action="store_true", dest="all_clusters",
                   help="Report every configured cluster.")
    p.add_argument("--gpu", default=None, metavar="TYPE",
                   help="GPU you intend to request (e.g. a100:1); adds a routing hint "
                        "when none are free.")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Machine-readable output, for tools that route on it.")
    p.add_argument("--quota", action="store_true",
                   help="Also run the cluster's quota/allowance commands. Off by "
                        "default: their output is site-defined and often contains "
                        "personal details (names, emails, group members), which "
                        "should not be pulled in on every routine check.")
    _add_cluster_flag(p)

    # doctor
    sub.add_parser("doctor",
                   help="How this tool is installed, and where it reads config from.")

    # clusters
    sub.add_parser("clusters", help="List configured clusters.")

    # check
    p = sub.add_parser("check", help="Test SSH connectivity and remote path for a cluster.")
    _add_cluster_flag(p)

    # init
    p = sub.add_parser("init", help="Create or migrate the cluster registry.")
    p.add_argument("--migrate", action="store_true",
                   help="Import existing hpc-submit / rsyncer configs.")

    return parser


def cmd_clusters() -> None:
    default, clusters = list_clusters()
    print(f"Registry: {len(clusters)} cluster(s) (default: {default or '—'})\n")
    for c in clusters:
        mark = "*" if c.name == default else " "
        print(f" {mark} {c.name}")
        print(f"      host:         {c.host}")
        print(f"      jobs_dir:     {c.jobs_dir}")
        print(f"      search_paths: {', '.join(c.search_paths) or '—'}")
        if c.ssh_stderr_filter:
            print(f"      ssh filter:   {c.ssh_stderr_filter!r}")
        if c.notes_file:
            print(f"      notes:        {c.notes_file}")
        print()


def cmd_preflight(args) -> int:
    """Report submission readiness for one cluster or all of them."""
    import json as _json

    from .preflight import gather
    from .preflight_report import render, render_routing_hint

    if args.all_clusters:
        names = sorted(load_registry()["clusters"])
        clusters = [resolve_cluster(n) for n in names]
    else:
        clusters = [resolve_cluster(args.cluster)]

    reports = []
    for cluster in clusters:
        if not args.quota:
            cluster.quota_commands = []
        reports.append(gather(cluster))

    if args.as_json:
        print(_json.dumps(reports if args.all_clusters else reports[0], indent=2))
    else:
        for i, report in enumerate(reports):
            if i:
                print()
            print(render(report, show_quota=args.quota))
            if args.gpu and report["reachable"]:
                hint = render_routing_hint(report, args.gpu)
                if hint:
                    print(hint)

    # Non-zero when nothing is usable, so a script can gate a submit on it.
    return 0 if any(r["reachable"] for r in reports) else 1


def cmd_check(cluster_name: str | None) -> int:
    cluster = resolve_cluster(cluster_name)
    print(f"Cluster '{cluster.name}' — checking SSH to '{cluster.host}'...")
    ok, msg = test_ssh_connection(cluster.host)
    if ok:
        print(f"  [PASS] {msg}")
    else:
        print("  [FAIL]")
        for line in msg.splitlines():
            print(f"    {line}")
        return 1
    base = cluster.jobs_dir_x
    print(f"Checking remote path '{base}'...")
    path_ok, path_msg = test_remote_path(cluster.host, base)
    if path_ok:
        print(f"  [PASS] {path_msg}")
    else:
        print("  [FAIL]")
        for line in path_msg.splitlines():
            print(f"    {line}")
        return 1
    print("\nAll checks passed.")
    return 0


def main() -> None:
    # Local imports so `hpcjob clusters/init` work even if a cluster is unreachable.
    from .pull import pull, show_recent
    from .submit import cancel_job, check_job_status, submit

    try:
        parser = build_parser()
        args = parser.parse_args()

        if args.command is None:
            parser.print_help()
            sys.exit(0)

        if args.command == "init":
            interactive_init(migrate=args.migrate)
            return
        if args.command == "doctor":
            from .build import report
            print(report(__version__))
            return
        if args.command == "clusters":
            cmd_clusters()
            return
        if args.command == "check":
            sys.exit(cmd_check(args.cluster))
        if args.command == "preflight":
            sys.exit(cmd_preflight(args))

        if args.command == "submit":
            if not args.job_script.exists():
                parser.error(f"job script not found: {args.job_script}")
            if args.job_script.suffix != ".sh":
                parser.error(f"job script must be a .sh file: {args.job_script}")
            for f in args.files:
                if not f.exists():
                    parser.error(f"file not found: {f}")
            cluster = resolve_cluster(args.cluster)
            submit(cluster, args.job_script, args.jobname, args.files, args.overwrite,
                   jobs_dir=getattr(args, "jobs_dir", None))
            return

        if args.command == "pull":
            cluster = resolve_cluster(args.cluster)
            pull(cluster, args.target, filter_exts=args.filter_exts, yes=args.yes,
                 dest=args.dest, path_prefix=args.path_prefix)
            return

        if args.command == "recent":
            cluster = resolve_cluster(args.cluster)
            show_recent(cluster, args.n, use_slurm=not args.all_paths,
                        slurm_days=args.days, yes=args.yes)
            return

        if args.command == "status":
            cluster = resolve_cluster(args.cluster)
            check_job_status(cluster, args.job_id)
            return

        if args.command == "cancel":
            cluster = resolve_cluster(args.cluster)
            for jid in args.job_id:
                cancel_job(cluster, jid)
            return

    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
