"""`hpcjob preflight` — can I submit here, and where should the job go?

Answers, in one call: is the cluster up, am I over quota, what is my scheduling
standing, and which GPUs are actually free right now.

Design rule: **the Slurm layer is generic, the site layer is config.**

  - Queue, GPU availability and fairshare come from `scontrol`, `squeue` and
    `sshare`, which are standard Slurm and identical on any cluster. Because
    they are standard, their output is parsed into structure that other tools
    can route on.
  - Anything site-specific — the status page URL, which quota command a site
    provides (`myquota`/`accinfo` vs `hbquota` vs something else) — is declared
    per cluster in `clusters.yaml` and its output is passed through **verbatim**,
    never parsed. Parsing it is what would force site knowledge into this file.

That split is why adding a cluster is a config edit, not a patch.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import Cluster
from .ssh import filter_stderr

MARK = "@@HPCJOB@@"          # section delimiter in the single remote call
STATUS_WORDS = ("maintenance", "outage", "incident", "degraded", "unavailable",
                "down", "disruption", "operational")


# ---------------------------------------------------------------------------
# Site status page (config-declared URL; content read, never assumed)
# ---------------------------------------------------------------------------


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6])>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def fetch_status(url: str, timeout: int = 10) -> dict[str, Any]:
    """Fetch a status page and pull out the lines that mention a service state.

    No per-site parser: different sites run Staytus, Confluence, or hand-written
    HTML with nothing in common. Extracting the status-bearing lines and letting
    the caller judge is both simpler and more honest than pretending to
    understand each layout.
    """
    out: dict[str, Any] = {"url": url, "reachable": False, "lines": [], "error": None}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hpcjob-preflight"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["error"] = str(exc)
        return out

    out["reachable"] = True
    text = _html_to_text(html)
    seen: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        if any(w in low for w in STATUS_WORDS) and len(line) < 200:
            if line not in seen:
                seen.append(line)
    out["lines"] = seen[:25]
    # A page saying only "operational" everywhere is the good case.
    bad = [ln for ln in seen if any(w in ln.lower() for w in STATUS_WORDS[:-1])]
    out["looks_degraded"] = bool(bad)
    return out


# ---------------------------------------------------------------------------
# Slurm layer — standard everywhere, so safe to parse
# ---------------------------------------------------------------------------


@dataclass
class GpuAvailability:
    partition: str
    gpu_type: str
    total: int = 0
    used: int = 0
    nodes_up: int = 0
    pending: int = 0

    @property
    def free(self) -> int:
        return max(self.total - self.used, 0)

    def as_dict(self) -> dict[str, Any]:
        return {"partition": self.partition, "gpu_type": self.gpu_type,
                "total": self.total, "used": self.used, "free": self.free,
                "nodes_up": self.nodes_up, "pending": self.pending}


def _gres_counts(text: str) -> dict[str, int]:
    """Parse `gpu:a100:4` / `gres/gpu:a100=2` / `gres/gpu=2` into {type: count}."""
    out: dict[str, int] = {}
    for m in re.finditer(r"gpu:([a-z0-9_.\-]+):(\d+)", text, re.I):
        out[m.group(1).lower()] = out.get(m.group(1).lower(), 0) + int(m.group(2))
    if out:
        return out
    for m in re.finditer(r"gres/gpu:([a-z0-9_.\-]+)=(\d+)", text, re.I):
        out[m.group(1).lower()] = out.get(m.group(1).lower(), 0) + int(m.group(2))
    if out:
        return out
    m = re.search(r"gres/gpu=(\d+)", text, re.I)
    if m:
        out["gpu"] = int(m.group(1))
    else:
        m = re.search(r"(?<![\w:])gpu:(\d+)(?![\w:])", text, re.I)
        if m:
            out["gpu"] = int(m.group(1))
    return out


# Node states that can actually accept work now or shortly.
_USABLE = ("idle", "mixed", "alloc", "completing", "reserved")


def parse_nodes(
    scontrol_out: str,
    ignore_partitions: list[str] | None = None,
) -> tuple[dict[tuple[str, str], GpuAvailability], dict[str, dict[str, int]]]:
    """Capacity from `scontrol -o show node`.

    Returns per-(partition, gpu type) figures for display, **and** per-gpu-type
    totals counted over unique nodes.

    The two differ, and the difference matters: a node usually belongs to
    several partitions (on Snellius the same A100s serve both `gpu_a100` and
    `gpu_vis`), so summing partition rows counts those GPUs twice and overstates
    what is free. Routing decisions use the node-deduplicated totals.

    Nodes that are down/drained/failed are excluded: a GPU on a drained node is
    not capacity, and counting it would make a full partition look available.
    """
    import fnmatch

    ignore = list(ignore_partitions or [])
    avail: dict[tuple[str, str], GpuAvailability] = {}
    seen_nodes: set[str] = set()
    totals: dict[str, dict[str, int]] = {}

    for line in scontrol_out.splitlines():
        if "NodeName=" not in line:
            continue
        fields = dict(re.findall(r"(\w+)=([^\s]+)", line))
        state = fields.get("State", "").lower()
        if not any(s in state for s in _USABLE):
            continue
        partitions = [p for p in fields.get("Partitions", "").split(",") if p]
        # Partitions you cannot submit to are worse than useless here: a
        # reserved partition sitting fully idle reads as the obvious place to
        # send the job. Which ones those are is a site fact, so it comes from
        # config rather than from probing associations.
        if ignore:
            partitions = [p for p in partitions
                          if not any(fnmatch.fnmatch(p, pat) for pat in ignore)]
        if not partitions:
            continue
        cfg = _gres_counts(fields.get("Gres", "") or fields.get("CfgTRES", ""))
        used_all = _gres_counts(fields.get("AllocTRES", ""))
        if not cfg:
            continue

        node = fields.get("NodeName", "")
        first_sighting = node not in seen_nodes
        seen_nodes.add(node)

        for part in partitions:
            for gpu_type, total in cfg.items():
                used = used_all.get(
                    gpu_type, used_all.get("gpu", 0) if len(cfg) == 1 else 0)
                entry = avail.setdefault((part, gpu_type),
                                         GpuAvailability(part, gpu_type))
                entry.total += total
                entry.used += used
                entry.nodes_up += 1
                if first_sighting:
                    agg = totals.setdefault(gpu_type, {"total": 0, "used": 0,
                                                       "nodes": 0})
                    agg["total"] += total
                    agg["used"] += used
                    agg["nodes"] += 1
    for agg in totals.values():
        agg["free"] = max(agg["total"] - agg["used"], 0)
    return avail, totals


def parse_pending(squeue_out: str) -> dict[str, int]:
    """Pending job count per partition — the queue pressure behind a choice."""
    counts: dict[str, int] = {}
    for line in squeue_out.splitlines():
        part = line.strip().split()[0] if line.strip() else ""
        if part:
            for p in part.split(","):
                counts[p] = counts.get(p, 0) + 1
    return counts


def parse_fairshare(sshare_out: str) -> list[dict[str, Any]]:
    """`sshare -P` rows for this user: raw usage and the resulting factor."""
    rows: list[dict[str, Any]] = []
    for line in sshare_out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not parts[1]:
            continue
        row = {"account": parts[0], "user": parts[1]}
        for key, idx in (("raw_usage", 2), ("norm_shares", 3),
                         ("effectv_usage", 4), ("fairshare", 5)):
            if idx < len(parts):
                row[key] = parts[idx]
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# One remote call
# ---------------------------------------------------------------------------


def _remote_script(quota_commands: list[str]) -> str:
    """Everything gathered in a single ssh round-trip, delimited by MARK."""
    parts = [
        f'echo "{MARK}nodes"; scontrol -o show node 2>/dev/null',
        f'echo "{MARK}pending"; squeue -h -t PD -o "%P" 2>/dev/null',
        f'echo "{MARK}fairshare"; sshare -U -P -n '
        f'-o Account,User,RawUsage,NormShares,EffectvUsage,FairShare 2>/dev/null',
    ]
    if quota_commands:
        # Quota tools are written for humans at a terminal and several size their
        # output with `tput cols`, which exits non-zero when TERM is unset -- as
        # it is over a non-interactive ssh. Habrok's `hbquota` crashes outright
        # without this, dumping a Python traceback where the quota should be.
        parts.append("export TERM=${TERM:-xterm} COLUMNS=${COLUMNS:-100}")
    for cmd in quota_commands:
        # Verbatim: the point is not to parse site-specific quota output.
        safe = cmd.replace('"', '\\"')
        parts.append(f'echo "{MARK}quota:{cmd}"; {safe} 2>&1')
    return "; ".join(parts)


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Remove colour/bold escapes from passed-through output.

    Not parsing -- the text is unchanged. Those tools colour their output for a
    terminal, and the escapes are noise in a log or an agent's context.
    """
    return _ANSI.sub("", text)


def _split_sections(out: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in out.splitlines():
        if line.startswith(MARK):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[len(MARK):].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def gather(cluster: Cluster, *, timeout: int = 60) -> dict[str, Any]:
    """Collect everything preflight reports for one cluster."""
    result: dict[str, Any] = {
        "cluster": cluster.name,
        "host": cluster.host,
        "reachable": False,
        "ssh_error": None,
        "status": None,
        "gpus": [],
        "pending": {},
        "fairshare": [],
        "quota": {},
    }

    script = _remote_script(cluster.quota_commands)
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             cluster.host, script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["ssh_error"] = f"timed out after {timeout}s"
        proc = None
    except OSError as exc:
        result["ssh_error"] = str(exc)
        proc = None

    if proc is not None and proc.returncode == 0:
        result["reachable"] = True
        sections = _split_sections(proc.stdout)
        nodes, totals = parse_nodes(sections.get("nodes", ""),
                                    cluster.ignore_partitions)
        pending = parse_pending(sections.get("pending", ""))
        for (part, _gpu), entry in nodes.items():
            entry.pending = pending.get(part, 0)
        result["gpus"] = [e.as_dict() for e in sorted(
            nodes.values(), key=lambda e: (e.partition, e.gpu_type))]
        result["gpu_totals"] = totals
        result["pending"] = pending
        result["fairshare"] = parse_fairshare(sections.get("fairshare", ""))
        result["quota"] = {k.split(":", 1)[1]: _strip_ansi(v)
                           for k, v in sections.items() if k.startswith("quota:")}
    elif proc is not None:
        result["ssh_error"] = filter_stderr(
            proc.stderr, cluster.ssh_stderr_filter).strip() or f"exit {proc.returncode}"

    # Only consult the status page when something is wrong, or when asked to.
    # An ssh failure is the case where its answer changes the diagnosis
    # completely: a cluster in maintenance refuses connections in ways that
    # look like a credential problem.
    if cluster.status_url and not result["reachable"]:
        result["status"] = fetch_status(cluster.status_url)

    return result


# ---------------------------------------------------------------------------
# Routing support (consumed by the job generators)
# ---------------------------------------------------------------------------


def free_gpus(report: dict[str, Any], gpu_type: str) -> int:
    """Free GPUs of a type, counted over unique nodes.

    Uses the node-deduplicated totals, not the sum of partition rows: shared
    nodes appear in several partitions and summing them would overstate what is
    actually free.
    """
    gtype = gpu_type.split(":")[0].lower()
    return int(report.get("gpu_totals", {}).get(gtype, {}).get("free", 0))


def busiest(report: dict[str, Any], gpu_type: str) -> dict[str, Any] | None:
    gtype = gpu_type.split(":")[0].lower()
    matches = [g for g in report.get("gpus", []) if g["gpu_type"] == gtype]
    return max(matches, key=lambda g: g["pending"]) if matches else None
