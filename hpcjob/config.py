"""Shared cluster registry for hpcjob.

One file, `~/.config/hpcjob/clusters.yaml`, describes every cluster you can
reach. Each subcommand selects one with `--cluster NAME` (falling back to
`default_cluster`). The registry holds only *transport/location* facts — host,
where job dirs live, where to search for results, rsync flags. Tool-specific
knobs (e.g. boltz's GPU tiers / module loads) stay in their own tools' configs.

Schema
------
    default_cluster: habrok
    clusters:
      habrok:
        host: hpc                       # ssh alias or user@host (required)
        jobs_dir: /scratch/{user}/boltz_jobs   # base dir for submitted jobs (required)
        search_paths: [/scratch/{user}/]       # where `pull` searches by name
        rsync_flags: "-auz --info=progress2 -h"
        max_depth: 5
        ssh_stderr_filter: null         # drop stderr lines containing this substring
        user: null                      # optional; else resolved via `ssh host whoami`
        # `preflight` only, both optional:
        status_url: https://status.example.org/   # fetched when the cluster is
                                        # unreachable, to distinguish "down for
                                        # maintenance" from "your key is broken"
        quota_commands: [myquota]       # run with `preflight --quota`; output is
                                        # shown verbatim, never parsed
        ignore_partitions: [private*]   # optional manual override; partitions
                                        # barred by AllowAccounts/DenyAccounts
                                        # are dropped automatically

`{user}` in any path is replaced with the remote username (the `user:` field if
set, otherwise the result of `ssh <host> whoami`, resolved once and cached).

`status_url` and `quota_commands` are the only site-specific knowledge in the
tool, and they live here rather than in code precisely because they differ per
site: quota output has no common format across clusters, so it is passed
through for a human (or an agent) to read instead of being parsed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_DIR = Path.home() / ".config" / "hpcjob"
CONFIG_PATH = CONFIG_DIR / "clusters.yaml"

DEFAULT_RSYNC_FLAGS = "-auz --info=progress2 -h"
DEFAULT_MAX_DEPTH = 5


@dataclass
class Cluster:
    name: str
    host: str
    jobs_dir: str
    search_paths: list[str] = field(default_factory=list)
    rsync_flags: str = DEFAULT_RSYNC_FLAGS
    max_depth: int = DEFAULT_MAX_DEPTH
    ssh_stderr_filter: str | None = None
    user: str | None = None
    notes_file: str | None = None
    # `preflight` only. Site-specific by nature, so both are declared here
    # rather than known by the code: the URL is fetched and its text reported,
    # and each quota command's output is passed through verbatim.
    status_url: str | None = None
    quota_commands: list[str] = field(default_factory=list)
    # Partitions you cannot submit to (glob patterns). Reserved ones sit
    # idle and would otherwise read as the best place to send a job.
    ignore_partitions: list[str] = field(default_factory=list)

    # ---- {user} substitution -------------------------------------------------
    _resolved_user: str | None = field(default=None, repr=False)

    def _remote_user(self) -> str:
        if self.user:
            return self.user
        if self._resolved_user is None:
            result = subprocess.run(
                ["ssh", self.host, "whoami"], capture_output=True, text=True,
            )
            self._resolved_user = result.stdout.strip() or os.environ.get("LOGNAME", "")
        return self._resolved_user

    def expand(self, path: str) -> str:
        """Substitute {user} in a path (only touches ssh if {user} is present)."""
        if "{user}" not in path:
            return path
        return path.replace("{user}", self._remote_user())

    @property
    def jobs_dir_x(self) -> str:
        return self.expand(self.jobs_dir)

    @property
    def search_paths_x(self) -> list[str]:
        return [self.expand(p) for p in self.search_paths]


# ---------------------------------------------------------------------------
# Loading / resolving
# ---------------------------------------------------------------------------

def _die(msg: str) -> "None":
    print(msg, file=sys.stderr)
    sys.exit(1)


def load_registry() -> dict:
    if not CONFIG_PATH.exists():
        _die(
            f"No cluster registry found at {CONFIG_PATH}\n"
            "  Run: hpcjob init            # create one interactively\n"
            "  Or:  hpcjob init --migrate  # import existing hpc-submit/rsyncer configs"
        )
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text())
    except yaml.YAMLError:
        _die(f"Error: registry is not valid YAML: {CONFIG_PATH}")
    if not isinstance(raw, dict) or "clusters" not in raw:
        _die(f"Error: registry must be a mapping with a 'clusters' key: {CONFIG_PATH}")
    if not isinstance(raw["clusters"], dict) or not raw["clusters"]:
        _die(f"Error: registry has no clusters defined: {CONFIG_PATH}")
    return raw


def _cluster_from_raw(name: str, block: dict) -> Cluster:
    if "host" not in block or not block["host"]:
        _die(f"Error: cluster '{name}' is missing required field 'host'")
    if "jobs_dir" not in block or not block["jobs_dir"]:
        _die(f"Error: cluster '{name}' is missing required field 'jobs_dir'")
    return Cluster(
        name=name,
        host=str(block["host"]),
        jobs_dir=str(block["jobs_dir"]),
        search_paths=[str(p) for p in block.get("search_paths", [])],
        rsync_flags=str(block.get("rsync_flags", DEFAULT_RSYNC_FLAGS)),
        max_depth=int(block.get("max_depth", DEFAULT_MAX_DEPTH)),
        ssh_stderr_filter=(str(block["ssh_stderr_filter"])
                           if block.get("ssh_stderr_filter") else None),
        user=(str(block["user"]) if block.get("user") else None),
        notes_file=(str(block["notes_file"]) if block.get("notes_file") else None),
        status_url=(str(block["status_url"]) if block.get("status_url") else None),
        quota_commands=[str(c) for c in block.get("quota_commands", [])],
        ignore_partitions=[str(p) for p in block.get("ignore_partitions", [])],
    )


def resolve_cluster(name: str | None) -> Cluster:
    """Return the requested cluster, or the default if name is None."""
    raw = load_registry()
    clusters = raw["clusters"]
    if name is None:
        name = raw.get("default_cluster")
        if not name:
            if len(clusters) == 1:
                name = next(iter(clusters))
            else:
                _die(
                    "Error: no --cluster given and no 'default_cluster' set.\n"
                    f"  Available: {', '.join(sorted(clusters))}"
                )
    if name not in clusters:
        _die(
            f"Error: unknown cluster '{name}'.\n"
            f"  Available: {', '.join(sorted(clusters))}"
        )
    return _cluster_from_raw(name, clusters[name])


def list_clusters() -> tuple[str | None, list[Cluster]]:
    raw = load_registry()
    default = raw.get("default_cluster")
    out = [_cluster_from_raw(n, b) for n, b in sorted(raw["clusters"].items())]
    return default, out


def save_registry(raw: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(raw, default_flow_style=False, sort_keys=False))


# ---------------------------------------------------------------------------
# init / migrate
# ---------------------------------------------------------------------------

def migrate_from_legacy() -> dict | None:
    """Build a registry block from existing hpc-submit + rsyncer configs.

    Returns a single-cluster registry (named 'default') seeded from whatever
    legacy config files are present, or None if neither exists.
    """
    hs_path = Path.home() / ".config" / "hpc-submit" / "config.yaml"
    rs_path = Path.home() / ".config" / "rsyncer" / "config.json"

    host = jobs_dir = None
    search_paths: list[str] = []
    rsync_flags = DEFAULT_RSYNC_FLAGS
    max_depth = DEFAULT_MAX_DEPTH

    if hs_path.exists():
        hs = yaml.safe_load(hs_path.read_text()) or {}
        host = hs.get("remote_host") or host
        jobs_dir = hs.get("remote_base_path") or jobs_dir
    if rs_path.exists():
        rs = json.loads(rs_path.read_text())
        host = host or rs.get("server")
        search_paths = rs.get("search_paths", search_paths)
        rsync_flags = rs.get("rsync_flags", rsync_flags)
        max_depth = rs.get("max_depth", max_depth)

    if not host or not jobs_dir:
        return None

    return {
        "default_cluster": "default",
        "clusters": {
            "default": {
                "host": host,
                "jobs_dir": jobs_dir.rstrip("/"),
                "search_paths": search_paths or [jobs_dir.rstrip("/")],
                "rsync_flags": rsync_flags,
                "max_depth": max_depth,
            }
        },
    }


def interactive_init(migrate: bool = False) -> None:
    if CONFIG_PATH.exists():
        print(f"Registry already exists: {CONFIG_PATH}")
        if input("  Overwrite? [y/N]: ").strip().lower() != "y":
            print("Kept existing registry.")
            return

    if migrate:
        raw = migrate_from_legacy()
        if raw is None:
            print("No legacy hpc-submit/rsyncer config found to migrate.")
        else:
            save_registry(raw)
            print(f"Migrated legacy config into {CONFIG_PATH}")
            print("Edit that file to rename the cluster and add more.")
            return

    print("hpcjob: cluster registry setup")
    print("=" * 40)
    name = input("Cluster name [default]: ").strip() or "default"
    host = input("SSH target (alias or user@host) [hpc]: ").strip() or "hpc"
    jobs_dir = input("Remote base path for job dirs: ").strip()
    if not jobs_dir:
        _die("Error: a jobs_dir is required.")
    raw_paths = input(f"Search paths for `pull` (comma-sep) [{jobs_dir}]: ").strip()
    search_paths = [p.strip() for p in raw_paths.split(",") if p.strip()] or [jobs_dir]
    raw = {
        "default_cluster": name,
        "clusters": {
            name: {
                "host": host,
                "jobs_dir": jobs_dir.rstrip("/"),
                "search_paths": search_paths,
                "rsync_flags": DEFAULT_RSYNC_FLAGS,
                "max_depth": DEFAULT_MAX_DEPTH,
            }
        },
    }
    save_registry(raw)
    print(f"\nRegistry written to {CONFIG_PATH}")
    print("Add more clusters by editing that file.")
