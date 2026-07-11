"""Find a remote directory by name and rsync it down — the `pull` and `recent`
subcommands. Ported from the standalone `rsyncer` tool; server / search paths /
rsync flags now come from a resolved Cluster.
"""

from __future__ import annotations

import os
import subprocess
import sys

from .config import Cluster
from .ssh import filter_stderr


def _ssh_capture(cluster: Cluster, remote_cmd: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["ssh", cluster.host, remote_cmd],
                            capture_output=True, text=True)
    result.stdout = filter_stderr(result.stdout, cluster.ssh_stderr_filter)
    result.stderr = filter_stderr(result.stderr, cluster.ssh_stderr_filter)
    return result


def find_on_server(cluster: Cluster, folder_name: str) -> list[str]:
    search_paths = " ".join(cluster.search_paths_x)
    cmd = (f"find {search_paths} -maxdepth {cluster.max_depth} -type d "
           f"-name '{folder_name}' 2>/dev/null")
    print(f"Searching for '{folder_name}' on {cluster.host}...")
    print(f"  ssh {cluster.host} \"{cmd}\"")
    result = _ssh_capture(cluster, cmd)
    if result.returncode != 0 and result.stderr.strip():
        print(f"SSH error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return [p.strip() for p in result.stdout.strip().splitlines() if p.strip()]


def pick_path(paths: list[str]) -> str:
    print(f"\nFound {len(paths)} matches:\n")
    for i, p in enumerate(paths, 1):
        print(f"  {i}) {p}")
    while True:
        choice = input(f"\nSelect [1-{len(paths)}]: ").strip()
        try:
            idx = int(choice)
            if 1 <= idx <= len(paths):
                return paths[idx - 1]
        except ValueError:
            pass
        print("Invalid selection, try again.")


def get_slurm_workdirs(cluster: Cluster, days: int = 29) -> list[str] | None:
    cmd = (f"sacct -S $(date +%Y-%m-%d -d '-{days} days') "
           f"--format=WorkDir%199 --noheader | sort -u")
    print(f"Querying SLURM for job directories from the last {days} days...")
    print(f"  ssh {cluster.host} \"{cmd}\"")
    result = _ssh_capture(cluster, cmd)
    if result.returncode != 0 and result.stderr.strip():
        print(f"sacct error: {result.stderr.strip()}", file=sys.stderr)
        return None
    dirs = list(dict.fromkeys(
        p.strip() for p in result.stdout.strip().splitlines() if p.strip()))
    return dirs or None


def show_recent(cluster: Cluster, n: int, use_slurm: bool = True,
                slurm_days: int = 29, yes: bool = False) -> None:
    if use_slurm:
        slurm_dirs = get_slurm_workdirs(cluster, slurm_days)
        if slurm_dirs:
            print(f"\nSLURM working directories ({len(slurm_dirs)}):\n")
            for i, d in enumerate(slurm_dirs, 1):
                print(f"  {i}) {d}")
            exclude = "" if yes else input(
                "\nExclude any? Enter numbers to remove (e.g. 1,3) or Enter to keep all: ").strip()
            if exclude:
                try:
                    remove = {int(x.strip()) for x in exclude.split(",") if x.strip()}
                    slurm_dirs = [d for i, d in enumerate(slurm_dirs, 1) if i not in remove]
                    print(f"Searching {len(slurm_dirs)} directories.\n")
                except ValueError:
                    print("Could not parse input, keeping all directories.\n")
            if not slurm_dirs:
                search_paths = " ".join(cluster.search_paths_x)
            else:
                slurm_dirs.sort()
                pruned: list[str] = []
                for d in slurm_dirs:
                    if not any(d.startswith(parent + "/") for parent in pruned):
                        pruned.append(d)
                if len(pruned) < len(slurm_dirs):
                    print(f"Pruned {len(slurm_dirs) - len(pruned)} subdirs covered by parents.")
                search_paths = " ".join(f"'{d}'" for d in pruned)
        else:
            print("No SLURM jobs found, falling back to configured search paths.\n")
            search_paths = " ".join(cluster.search_paths_x)
    else:
        search_paths = " ".join(cluster.search_paths_x)

    cmd = (f"find {search_paths} -maxdepth {cluster.max_depth} -type f "
           f"-printf '%T@ %T+ %h\\n' 2>/dev/null "
           f"| sort -rn | awk '!seen[$3]++' | head -n {n}")
    print(f"Finding {n} most recently modified directories on {cluster.host}...")
    print(f"  ssh {cluster.host} \"{cmd}\"")
    result = _ssh_capture(cluster, cmd)
    if result.returncode != 0 and result.stderr.strip():
        print(f"SSH error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    if not lines:
        print("No results found.")
        return
    print(f"\n{'#':>3}  {'Last modified':<26} Path")
    print(f"{'':>3}  {'':─<26} {'':─<40}")
    for i, line in enumerate(lines, 1):
        parts = line.split(None, 2)
        if len(parts) == 3:
            _, timestamp, path = parts
            timestamp = timestamp.replace("+", " ")[:16]
            print(f"{i:>3}) {timestamp:<26} {path}")
    print()


def pick_extensions(cluster: Cluster, server_path: str) -> list[str] | None:
    cmd = f"find '{server_path}' -type f | sed 's/.*\\.//' | sort | uniq -c | sort -rn"
    print(f"\nFinding file types in {server_path}...")
    print(f"  ssh {cluster.host} \"{cmd}\"")
    result = _ssh_capture(cluster, cmd)
    if result.returncode != 0 and result.stderr.strip():
        print(f"SSH error: {result.stderr.strip()}", file=sys.stderr)
        return None
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    if not lines:
        print("No files found.")
        return None
    extensions: list[str] = []
    print("\nFile types found:\n")
    for i, line in enumerate(lines, 1):
        parts = line.split(None, 1)
        if len(parts) == 2:
            count, ext = parts
            extensions.append(ext)
            print(f"  {i}) .{ext} ({count} files)")
    selection = input("\nInclude which? Enter numbers (e.g. 1,3,5) or Enter for all: ").strip()
    if not selection:
        return None
    try:
        idxs = {int(x.strip()) for x in selection.split(",") if x.strip()}
        chosen = [ext for i, ext in enumerate(extensions, 1) if i in idxs]
        if chosen:
            print(f"Filtering to: {', '.join('.' + e for e in chosen)}")
            return chosen
    except ValueError:
        print("Could not parse input, syncing all files.")
    return None


def do_rsync(cluster: Cluster, server_path: str, local_folder: str,
             extensions: list[str] | None = None, dest: str | None = None) -> None:
    remote = f"{cluster.host}:{server_path.rstrip('/')}/"
    local = f"{dest}/" if dest else f"./{local_folder}/"
    ext_filters = ""
    if extensions:
        includes = " ".join(f"--include='*.{ext}'" for ext in extensions)
        ext_filters = f" --include='*/' {includes} --exclude='*'"
    cmd = f"rsync {cluster.rsync_flags} --stats{ext_filters} {remote} {local}"
    print(f"\nRunning: {cmd}\n")
    subprocess.run(cmd, shell=True)


def pull(cluster: Cluster, target: str, *, filter_exts: bool = False, yes: bool = False,
         dest: str | None = None, path_prefix: str | None = None) -> None:
    input_arg = target.rstrip("/")
    folder_name = os.path.basename(input_arg)

    if input_arg.startswith("/"):
        chosen = input_arg
        print(f"Using remote path: {chosen}")
    else:
        paths = find_on_server(cluster, folder_name)
        if path_prefix:
            before = len(paths)
            paths = [p for p in paths if p.startswith(path_prefix)]
            print(f"Filtered {before} -> {len(paths)} match(es) with prefix {path_prefix!r}")
        if not paths:
            print(f"No directories named '{folder_name}' found on {cluster.host}.",
                  file=sys.stderr)
            sys.exit(1)
        elif len(paths) == 1:
            chosen = paths[0]
            if yes:
                print(f"Auto-selected: {chosen}")
            else:
                answer = input(f"\nFound: {chosen}\nSync this? [Y/n] ").strip().lower()
                if answer and answer != "y":
                    print("Aborted.")
                    sys.exit(0)
        else:
            if yes or not sys.stdin.isatty():
                chosen = paths[0]
                print(f"Auto-selected (first match): {chosen}")
            else:
                chosen = pick_path(paths)

    extensions = pick_extensions(cluster, chosen) if filter_exts else None

    local_dir = dest if dest else folder_name
    if not os.path.isdir(local_dir):
        if yes:
            os.makedirs(local_dir)
            print(f"Created {local_dir}/")
        else:
            answer = input(
                f"Local directory './{local_dir}' does not exist. Create it? [Y/n] "
            ).strip().lower()
            if answer and answer != "y":
                print("Aborted.")
                sys.exit(0)
            os.makedirs(local_dir)
            print(f"Created ./{local_dir}/")

    do_rsync(cluster, chosen, folder_name, extensions=extensions, dest=dest)
