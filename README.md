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

Without `--cluster`, the registry's `default_cluster` is used.
