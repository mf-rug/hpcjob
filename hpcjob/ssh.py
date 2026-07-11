"""Shared SSH helpers: connectivity diagnostics, remote-path tests, and a
stderr-noise filter (e.g. Snellius' post-quantum key-exchange warning).

All diagnosis comes from ssh's exit code and stderr — no ~/.ssh files are read.
"""

from __future__ import annotations

import subprocess


def filter_stderr(text: str, needle: str | None) -> str:
    """Drop stderr lines containing `needle` (case-insensitive).

    Used to suppress benign, always-printed SSH advisories (e.g. Snellius emits
    a post-quantum key-exchange warning on every connection) that would
    otherwise pollute parsed output. Returns `text` unchanged if needle is None.
    """
    if not needle:
        return text
    needle_l = needle.lower()
    kept = [ln for ln in text.splitlines() if needle_l not in ln.lower()]
    return "\n".join(kept)


def run_ssh(
    host: str,
    remote_cmd: str,
    *,
    stderr_filter: str | None = None,
    timeout: int | None = None,
    batch: bool = False,
) -> subprocess.CompletedProcess:
    """Run a single remote command over SSH and return the CompletedProcess.

    stderr is passed through `filter_stderr` so callers that parse output do not
    trip over benign advisories. `batch=True` adds BatchMode/ConnectTimeout for
    non-interactive probes.
    """
    cmd = ["ssh"]
    if batch:
        cmd += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    cmd += [host, remote_cmd]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if stderr_filter:
        result.stderr = filter_stderr(result.stderr, stderr_filter)
    return result


def test_ssh_connection(host: str) -> tuple[bool, str]:
    """Test SSH connectivity in batch mode. Returns (success, actionable message)."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
             "echo __hpcjob_ok__"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return (False,
            "SSH connection timed out after 15 seconds.\n"
            "  - The hostname may be wrong or the server unreachable\n"
            "  - A firewall or VPN may be blocking the connection")
    except FileNotFoundError:
        return (False, "ssh command not found. Is OpenSSH installed?")

    if result.returncode == 0 and "__hpcjob_ok__" in result.stdout:
        return (True, f"Connected to '{host}'.")

    stderr = result.stderr.strip().lower()

    if "could not resolve hostname" in stderr:
        return (False,
            f"Hostname not found: '{host}'\n"
            "  - Check for typos in the hostname\n"
            "  - If using an SSH alias, ensure it is defined in ~/.ssh/config")
    if "connection refused" in stderr:
        return (False,
            f"Connection refused by '{host}'\n"
            "  - The SSH port may be wrong (default: 22)\n"
            "  - The server may be down or decommissioned\n"
            "  - Try a different login node if available")
    if "host key verification failed" in stderr:
        return (False,
            f"Host key verification failed for '{host}'\n"
            "  Your ~/.ssh/known_hosts has a stale entry for this server\n"
            "  (common after a cluster reinstall).\n"
            "  Fix: ssh-keygen -R <hostname>   then connect manually once\n"
            "  to accept the new host key.")
    if "permission denied" in stderr and "keyboard-interactive" in stderr:
        return (False,
            f"Authentication to '{host}' requires 2FA (keyboard-interactive).\n"
            "  Batch SSH calls cannot prompt for a 2FA code.\n"
            "  Fix: set up SSH ControlMaster multiplexing.\n\n"
            "  1. Add to ~/.ssh/config:\n\n"
            f"     Host {host}\n"
            "         HostName <cluster-login-node>\n"
            "         User <your-username>\n"
            "         ControlMaster auto\n"
            "         ControlPath ~/.ssh/sockets/%r@%h-%p\n"
            "         ControlPersist 4h\n\n"
            "  2. Create the socket directory:\n"
            "         mkdir -p ~/.ssh/sockets\n\n"
            "  3. Connect once interactively:\n"
            f"         ssh {host}\n\n"
            "  After authenticating, all subsequent ssh/rsync/hpcjob calls\n"
            "  reuse the authenticated connection for 4 hours.")
    if "permission denied" in stderr and "publickey" in stderr:
        return (False,
            f"Public key authentication failed for '{host}'\n"
            "  - Check that your key is loaded: ssh-add -l\n"
            "  - Check that IdentityFile is set correctly in ~/.ssh/config\n"
            "  - Verify the public key is in authorized_keys on the server")
    if "permission denied" in stderr:
        return (False,
            f"Permission denied connecting to '{host}'\n"
            f"  SSH error: {result.stderr.strip()}")

    return (False,
        f"SSH connection to '{host}' failed (exit code {result.returncode}).\n"
        f"  SSH error: {result.stderr.strip()}\n"
        f"  Try connecting manually: ssh {host}")


def test_remote_path(host: str, path: str) -> tuple[bool, str]:
    """Test that a remote path exists (or can be created) and is writable."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
             f"mkdir -p {path} && test -w {path} && echo __path_ok__"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return (False, "Could not test remote path (SSH failed).")

    if result.returncode == 0 and "__path_ok__" in result.stdout:
        return (True, f"Remote path '{path}' is accessible and writable.")
    return (False,
        f"Remote path '{path}' is not writable or could not be created.\n"
        f"  SSH error: {result.stderr.strip()}\n"
        "  Ensure the directory exists and you have write permissions.")
