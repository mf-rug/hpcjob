# hpcjob

One tool to **submit** SLURM jobs to and **pull** results from remote HPC
clusters over SSH. Supersedes the separate `hpc-submit` (upload + sbatch) and
`rsyncer` (find-by-name + download) tools, and adds **multi-cluster** support:
every subcommand takes `--cluster NAME`, resolved from a shared registry.

## Install

```bash
pip install -e .        # or: pipx install .
```

This installs three entry points:

| Command | Purpose |
|---|---|
| `hpcjob` | the merged tool (use this going forward) |
| `hpc-submit` | back-compat shim → `hpcjob submit` (and `--status`/`--cancel`/`--check`) |
| `rsyncer` | back-compat shim → `hpcjob pull` (and `--recent`) |

The shims let existing habits and scripts keep working. If a stale
`rsyncer`/`hpc-submit` from the old standalone tools still shadows the shim
(via PATH or a shell alias), remove it so the shim wins.

## Configure

Clusters live in `~/.config/hpcjob/clusters.yaml`:

```yaml
default_cluster: mycluster
clusters:
  mycluster:
    host: mylogin                 # ssh alias or user@host   (required)
    jobs_dir: /scratch/{user}/jobs  # base dir for submitted jobs (required)
    search_paths:                 # where `pull` searches by name
      - /scratch/{user}/
    rsync_flags: "-auz --info=progress2 -h"
    max_depth: 5
    ssh_stderr_filter: null       # drop stderr lines containing this substring
    user: null                    # optional; else resolved via `ssh host whoami`
    notes_file: null              # optional per-cluster operational notes (*.md)

    # used by `hpcjob preflight`, both optional:
    status_url: https://status.example.org/   # fetched when the cluster is
                                  # unreachable, so a maintenance window is not
                                  # mistaken for a broken ssh key
    quota_commands: [myquota]     # e.g. [myquota, accinfo] or [hbquota]; run
                                  # with `preflight --quota`, output shown verbatim
```

`{user}` in any path is replaced with the remote username. Bootstrap the file
with `hpcjob init` (interactive) or `hpcjob init --migrate` (import an existing
`hpc-submit` + `rsyncer` config). Only transport/location facts belong here —
tool-specific knobs (e.g. a job generator's GPU tiers) stay in their own configs.

## Use

```bash
hpcjob submit myjob/job.sh                 # upload dir + sbatch
hpcjob status  30096786                    # squeue, then sacct
hpcjob cancel  30096786
hpcjob pull    myjob                        # find by name + rsync down
hpcjob pull    myjob --filter               # choose which file types to sync
hpcjob pull    /abs/remote/path --yes       # absolute path skips the search
hpcjob recent  10                           # recently active job dirs (sacct)
hpcjob clusters                             # list configured clusters
hpcjob check   --cluster snellius           # test SSH + remote path

hpcjob submit myjob/job.sh --cluster snellius   # target another cluster
```

### `preflight` — should I submit here at all?

```bash
hpcjob preflight --all               # every cluster: up? which GPUs are free?
hpcjob preflight --gpu a100:1        # + a note if that GPU type is saturated
hpcjob preflight --quota             # + the site's quota/allowance output
hpcjob preflight --json              # machine-readable, for tools that route on it
```

One ssh round-trip reporting reachability, free GPUs per partition, queue
pressure and fairshare standing. GPU totals are deduplicated by node, since a
node usually serves several partitions and summing partition rows overstates
what is free.

When a cluster is unreachable it fetches `status_url` and reports what the page
says. This is the point of the subcommand: **a cluster in maintenance refuses
ssh in ways that look like an authentication failure**, so the obvious response
is to start debugging keys instead of reading the status page.

`--quota` is opt-in rather than default. Quota output is site-defined and often
contains personal details (names, emails, group members), which have no place in
a routine availability check — and skipping it makes the call several times
faster.

It reports rather than recommends a substitute GPU: Slurm exposes GPU *names*,
not their VRAM, so this layer cannot tell an upgrade from a downgrade. Callers
that hold a capability ordering (a job generator's GPU tiers) can consume
`--json` and decide safely.

Exit status is non-zero when no cluster is reachable, so a script can gate a
submit on it.

Without `--cluster`, the registry's `default_cluster` is used.
