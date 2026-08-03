"""Human-readable rendering of a preflight gather()."""

from __future__ import annotations

from typing import Any


def _bar(free: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return ""
    filled = round((total - free) / total * width)
    return "[" + "#" * filled + "." * (width - filled) + "]"


def render(report: dict[str, Any], *, show_quota: bool = True) -> str:
    lines: list[str] = []
    name, host = report["cluster"], report["host"]

    if not report["reachable"]:
        lines.append(f"{name} ({host}): UNREACHABLE")
        if report.get("ssh_error"):
            for ln in str(report["ssh_error"]).splitlines()[:4]:
                lines.append(f"  ssh: {ln}")
        status = report.get("status")
        if status:
            if status.get("reachable") and status.get("lines"):
                verdict = ("reports a problem" if status.get("looks_degraded")
                           else "reports all services operational")
                lines.append(f"  status page {verdict}: {status['url']}")
                for ln in status["lines"][:8]:
                    lines.append(f"    | {ln}")
                if status.get("looks_degraded"):
                    lines.append("  -> the cluster is down or in maintenance; an "
                                 "auth-shaped ssh error here is a symptom, not a "
                                 "credential problem.")
            else:
                lines.append(f"  status page unreachable too ({status['url']})")
        else:
            lines.append("  no status_url configured for this cluster")
        return "\n".join(lines)

    lines.append(f"{name} ({host}): reachable")

    gpus = report.get("gpus", [])
    if gpus:
        lines.append("")
        lines.append("  GPUs available now")
        lines.append(f"    {'partition':<16} {'type':<16} {'free/total':>10}  "
                     f"{'pending':>7}")
        for g in gpus:
            lines.append(
                f"    {g['partition']:<16} {g['gpu_type']:<16} "
                f"{str(g['free']) + '/' + str(g['total']):>10}  {g['pending']:>7}  "
                f"{_bar(g['free'], g['total'])}"
            )
    else:
        lines.append("  (no GPU partitions reported)")

    fs = report.get("fairshare", [])
    if fs:
        lines.append("")
        lines.append("  Fairshare")
        for row in fs[:6]:
            bits = [f"{k}={row[k]}" for k in
                    ("effectv_usage", "fairshare", "raw_usage") if k in row]
            lines.append(f"    {row.get('account','?'):<20} {', '.join(bits)}")

    if show_quota and report.get("quota"):
        lines.append("")
        lines.append("  Quota / allowance (verbatim)")
        for cmd, out in report["quota"].items():
            lines.append(f"    $ {cmd}")
            for ln in out.splitlines():
                lines.append(f"      {ln}")

    return "\n".join(lines)


def render_routing_hint(report: dict[str, Any], wanted: str) -> str | None:
    """State what else is free when the requested GPU is saturated.

    Deliberately reports rather than recommends. Slurm exposes GPU *names*, not
    their VRAM, so this layer cannot tell whether another type is a step up or a
    step down -- and substituting a smaller card is not a slower job but an
    immediate OOM. The caller holds that ordering (the generators keep their GPU
    tiers ordered by capability) and is the only place a substitution can be
    decided safely.
    """
    gtype = wanted.split(":")[0].lower()
    totals = report.get("gpu_totals", {})
    mine = totals.get(gtype)
    if not mine:
        return f"  note: this cluster reports no GPUs of type {gtype!r}."
    if mine.get("free", 0) > 0:
        return None

    pending = max((g["pending"] for g in report.get("gpus", [])
                   if g["gpu_type"] == gtype), default=0)
    others = sorted(((t, v["free"]) for t, v in totals.items()
                     if t != gtype and v.get("free", 0) > 0),
                    key=lambda kv: -kv[1])
    head = (f"  note: all {mine['total']} {gtype} are busy"
            f"{f' ({pending} job(s) pending)' if pending else ''}.")
    if not others:
        return head + " No other GPU type is free either -- expect to queue."
    listing = ", ".join(f"{n} x {t}" for t, n in others)
    return (head + f" Free elsewhere: {listing}. Only substitute a type with at "
            f"least as much VRAM -- a smaller card OOMs rather than running slowly.")
