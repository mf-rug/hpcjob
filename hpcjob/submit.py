"""Upload a job directory and submit it with sbatch — the `submit` subcommand.

Ported from the standalone `hpc-submit` tool; the remote host / base path now
come from a resolved Cluster, and status/cancel filter benign SSH stderr noise.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .config import Cluster
from .ssh import filter_stderr, run_ssh


def parse_sbatch_directive(job_script: Path, directive: str) -> str | None:
    for line in job_script.read_text().splitlines():
        match = re.match(rf"^#SBATCH\s+--{directive}=(.+)", line)
        if match:
            return match.group(1).strip()
    return None


def sanitize_dir_name(name: str) -> str:
    sanitized = re.sub(r"\s+", "_", name)
    sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "", sanitized)
    return sanitized if sanitized else "job"


def remote_dir_exists(host: str, remote_path: str) -> bool:
    result = subprocess.run(["ssh", host, f"test -d {remote_path}"], capture_output=True)
    return result.returncode == 0


def resolve_remote_path(host: str, remote_path: str, overwrite: bool = False) -> str:
    if not remote_dir_exists(host, remote_path):
        return remote_path
    print(f"Remote directory already exists: {remote_path}")
    if overwrite:
        print("  Overwriting (--overwrite).")
        return remote_path
    choice = input("  [o]verwrite or [n]ew numbered directory? [o/n]: ").strip().lower()
    if choice == "o":
        return remote_path
    n = 1
    while True:
        candidate = f"{remote_path}_{n}"
        if not remote_dir_exists(host, candidate):
            print(f"  Using: {candidate}")
            return candidate
        n += 1


def create_remote_dir(host: str, remote_path: str) -> None:
    result = subprocess.run(["ssh", host, f"mkdir -p {remote_path}"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: failed to create remote directory {remote_path}", file=sys.stderr)
        print(f"  ssh stderr: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


def transfer_files(host: str, remote_path: str, job_script: Path,
                   extra_files: list[Path]) -> None:
    destination = f"{host}:{remote_path}/"
    job_dir = job_script.parent.resolve()
    print(f"Transferring contents of {job_dir}/ ...")
    cmd = [
        "rsync", "-avz", "--progress",
        "--exclude=output/", "--exclude=__pycache__/", "--exclude=*.pyc",
        str(job_dir) + "/", destination,
    ]
    if subprocess.run(cmd).returncode != 0:
        print("Error: rsync transfer failed", file=sys.stderr)
        sys.exit(1)
    for f in extra_files:
        if subprocess.run(["rsync", "-avz", "--progress", str(f), destination]).returncode != 0:
            print(f"Error: rsync transfer failed for {f}", file=sys.stderr)
            sys.exit(1)


def run_sbatch(cluster: Cluster, remote_path: str, script_name: str) -> int:
    result = run_ssh(cluster.host, f"cd {remote_path} && sbatch {script_name}",
                     stderr_filter=cluster.ssh_stderr_filter)
    if result.returncode != 0:
        print("Error: sbatch failed", file=sys.stderr)
        if result.stdout.strip():
            print(f"  stdout: {result.stdout.strip()}", file=sys.stderr)
        if result.stderr.strip():
            print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    stdout = filter_stderr(result.stdout.strip(), cluster.ssh_stderr_filter)
    try:
        return int(stdout.split()[-1])
    except (ValueError, IndexError):
        print(f"Error: could not parse job ID from sbatch output: {stdout!r}", file=sys.stderr)
        sys.exit(1)


def check_job_status(cluster: Cluster, job_id: int) -> None:
    result = run_ssh(cluster.host, f"squeue -j {job_id} --noheader -o '%T %r'",
                     stderr_filter=cluster.ssh_stderr_filter)
    if result.stdout.strip():
        print(f"Job {job_id}: {result.stdout.strip()}")
        return
    result = run_ssh(cluster.host,
                     f"sacct -j {job_id} --noheader -n -o 'State,ExitCode,Elapsed,NodeList'",
                     stderr_filter=cluster.ssh_stderr_filter)
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    print(f"Job {job_id}: {lines[0]}" if lines else f"Job {job_id}: not found")


def cancel_job(cluster: Cluster, job_id: int) -> None:
    result = run_ssh(cluster.host, f"scancel {job_id}",
                     stderr_filter=cluster.ssh_stderr_filter)
    if result.returncode != 0:
        print("Error: scancel failed", file=sys.stderr)
        if result.stderr.strip():
            print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    print(f"Job {job_id} cancelled.")


def check_output_dir(job_script: Path) -> str | None:
    output = parse_sbatch_directive(job_script, "output")
    if output is None:
        return None
    output_dir = str(Path(output).parent)
    return None if output_dir in (".", "") else output_dir


def submit(cluster: Cluster, job_script: Path, name: str | None,
           extra_files: list[Path], overwrite: bool = False) -> None:
    host = cluster.host
    base = cluster.jobs_dir_x

    if name is None:
        name = parse_sbatch_directive(job_script, "job-name")
        if name is None:
            print("Error: no #SBATCH --job-name= in script and no --jobname given",
                  file=sys.stderr)
            sys.exit(1)

    default_path = f"{base}/{sanitize_dir_name(name)}"
    output_dir = check_output_dir(job_script)
    if output_dir is not None:
        print(f"Found #SBATCH --output directory: {output_dir}")
        choice = input(f"  Use this instead of {default_path}? [y/n]: ").strip().lower()
        chosen = output_dir if choice == "y" else default_path
        remote_path = resolve_remote_path(host, chosen, overwrite=overwrite)
    else:
        remote_path = resolve_remote_path(host, default_path, overwrite=overwrite)

    print(f"Creating remote directory: {remote_path}")
    create_remote_dir(host, remote_path)
    print("Transferring files...")
    transfer_files(host, remote_path, job_script, extra_files)
    print("Submitting job...")
    job_id = run_sbatch(cluster, remote_path, job_script.name)

    print()
    print("-" * 40)
    print("Job submitted successfully.")
    print(f"  Job ID:           {job_id}")
    print(f"  Cluster:          {cluster.name}  ({host})")
    print(f"  Remote directory: {remote_path}")
    print()
    print(f"  Status:  hpcjob status {job_id} --cluster {cluster.name}")
    print(f"  Cancel:  hpcjob cancel {job_id} --cluster {cluster.name}")
    print(f"  Pull:    hpcjob pull {sanitize_dir_name(name)} --cluster {cluster.name}")
    print("-" * 40)
