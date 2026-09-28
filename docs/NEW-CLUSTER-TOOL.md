# Deploying a new model on the cluster

How the RFD3 and Boltz toolboxes were built, written down so the next one costs
days instead of weeks. Read this before starting a third.

The short version: **`hpcjob` is transport and never learns about your model;
a per-model generator authors inputs and job scripts and never learns about
ssh.** Everything below follows from that split.

---

## 0. The sequence

1. `hpcjob preflight --all` — is the cluster even up, what is free.
2. Decide whether you need a generator at all (§2).
3. Write it locally. Validate inputs **before** submission (§7.1).
4. Write a `HANDOFF.md`; a cluster-side agent installs and reports back (§5).
5. Fold its corrections into config, code **and** the handoff.
6. First real run. Expect three bugs. Fold those back too.
7. Calibrate the resource model by measurement (§6); label what is measured
   and what is extrapolated.

Steps 5–7 are where the value is. Steps 1–4 are the easy part.

---

## 1. What `hpcjob` owns

The cluster registry (`~/.config/hpcjob/clusters.yaml`) and everything that
touches ssh:

| | |
|---|---|
| `submit` | rsync a job dir, `sbatch` it. `--jobs-dir` overrides where it lands |
| `pull` | find results by name under `search_paths`, rsync down |
| `status` / `cancel` | `squeue` then `sacct`; `scancel` |
| `preflight` | up? which GPUs free? queue depth, fairshare, quota (`--quota`) |
| `doctor` | how this tool is installed, which interpreter owns it |

**It knows nothing about any model**, and must stay that way — that is what
lets two unrelated toolboxes share it. A new generator plugs in by emitting a
`job.sh` with `#SBATCH --job-name`, and by reading `preflight --json` if it
wants to route.

## 2. Does this need a dedicated tool?

**No** — just write a `job.sh` and use `hpcjob submit` — when the work is
"run this command on a cluster". One-off analyses, an existing CLI with
sensible defaults, anything you will run twice.

**Yes** — build a generator — when *two or more* of these hold:

- **Inputs need authoring.** A structured input file (YAML, JSON) whose schema
  is easy to get wrong and expensive to get wrong (a queue wait to find out).
- **Resources depend on the input.** GPU/walltime vary with sequence length,
  ligand count, batch size — so a human guesses, and guesses badly.
- **Outputs need post-processing** on the cluster, in the same job, while the
  data is local (parsing logs, chaining a second model, staging files).
- **You will run it tens of times** with varying parameters.

RFD3 hit all four. A one-off `foldseek` search hits none — that is a `job.sh`.

The cost of a generator is real: a second repo to version, a config schema, and
a new way for two tools to disagree (§7.5). Do not pay it for a script.

## 3. Config rule: derive > configure > hardcode

In order of preference:

1. **Derive it from a standard interface.** Slurm is the same everywhere:
   `scontrol show node`, `squeue`, `sshare`, and `AllowAccounts`/`DenyAccounts`
   on partitions. Anything you can read from Slurm, read — it needs no
   maintenance and works on a cluster nobody has seen yet.
2. **Put it in config** when it is a site fact with no standard interface:
   which quota command exists (`myquota`/`accinfo` vs `hbquota`), the status
   page URL, module names, venv paths, GPU tiers.
3. **Never hardcode** a cluster name, partition, path or command in Python.

A worked example of (1) beating (2): group-owned partitions were first handled
with a hand-written `ignore_partitions` list. Two hours later that was replaced
by reading `AllowAccounts` — now *any* reserved partition on *any* cluster
filters itself out, and a change in account membership is picked up for free.
The config key survives only as a manual override.

**Corollary — do not parse what you cannot generalise.** Quota output has no
common format across sites, so `preflight` runs the configured command and
passes its output through **verbatim**. The consumer is a human or an agent,
both of which read prose fine. Writing a parser per site is how site knowledge
leaks into a shared tool.

## 4. Shipping cluster-side code

Two things must run *on* the cluster: post-processing in the job, and anything
that needs filenames that do not exist until the job runs.

Copy the helper package **into every generated job directory** and invoke it
with `PYTHONPATH="$_job_dir"`. No cluster-side install, no version skew between
the generator and what the job runs.

Three rules for that package:

- **stdlib only.** It runs under the model's venv Python but must not import
  from it.
- **Never raise into the job.** The log parser is called from an exit trap; a
  crash there turns a successful run into a confusing failure.
- **Be idempotent.** Resubmits are common; staging steps get re-run.

## 5. The install handoff loop

The install is done by an agent *on the cluster*, not locally. Give it a
`HANDOFF.md` containing:

- target paths (venv, checkpoints, jobs dir) per cluster
- the install steps, with the reasoning, not just commands
- **a "known traps" section that accumulates across clusters** — this is the
  part that pays
- exactly what to report back, in the shape your config needs

Ask for the reply as a document. The Snellius report corrected three things the
brief got wrong, one of which was a live bug in the generator. Budget for the
handoff being wrong; say so in it, and ask explicitly for corrections.

**Fold the reply into three places**: the config, the code, and the handoff
itself. A correction that only reaches the config is lost for the next cluster.

## 6. Calibrating a resource model

Do not guess GPU tiers from spec sheets.

- Measure the **memory curve**, not one point. Peak VRAM is set by a single
  forward pass, so a 2-timestep probe measures the same peak as a 200-step one
  — probes cost seconds. A curve lets every other card be solved by
  extrapolation; one point does not.
- Read memory **in-process** (`torch.cuda.max_memory_allocated()`).
  `nvidia-smi --query-gpu` reports the parent device on MIG slices and includes
  other tenants.
- Run each probe in its **own process** so an OOM cannot poison the next.
- **Set the limit below the measured cliff.** Sizing usually keys on something
  adjacent to the real driver (design length vs token count; a ligand makes a
  job wider than its stated length), and the failure is abrupt — being 1% over
  costs the whole queued job, being conservative costs a bigger GPU.
- **Record which numbers are measured and which are extrapolated.** Future you
  will not remember, and will trust both equally.

`verify/bisect.sh` in the rfd3_cluster repo is a working, site-agnostic example.

## 7. Traps

Each of these cost a real run.

### 7.1 Validate inputs locally
If the model's input schema is a pydantic model with `extra="forbid"`, mirror
its field list and its required-field rules in the generator. A typo then fails
in a second instead of after a queue wait. Keep `prevalidate_inputs=True` (or
the equivalent) so the remaining mistakes fail in 49 seconds, before any GPU
work.

### 7.2 Verify against the installed package, not the docs
Read the schema out of `site-packages`. The published docs understated the
weight download by 43%, did not mention the output was gzipped, and showed a
CLI override syntax that does not run.

### 7.3 Output format assumptions
The single most expensive class. RFD3 writes `*.cif.gz`, never `.pdb`. Code
that globbed for `.cif`/`.pdb` would have reported **every successful run as a
failure**. Check what the model actually writes before writing the success
check.

### 7.4 Non-interactive ssh breaks human-facing tools
Site tools assume a terminal. `hbquota` sizes output with `tput cols`, which
exits non-zero when `TERM` is unset — so it crashed and the report showed a
Python traceback where the quota should be. Export `TERM`/`COLUMNS` before
running anything of that kind, and strip ANSI from what you pass through.

### 7.5 Install skew between tools
Independent repos mean "current here, behind there" is a *normal* state. A
generator that prints a command using a flag the installed transport lacks
surfaces the mismatch as "this command is wrong", which sends you looking in
the wrong place. Either probe (`tool subcmd --help`) or state a minimum
version. And make each tool able to say where its own code came from —
an editable install and a snapshot install report the same version string, but
only one tracks `git pull`. See `hpcjob doctor`.

### 7.6 Unpushed local changes
A flag added locally and never pushed makes every document that mentions it
fiction for anyone else. Before writing "the tool now does X", confirm X is on
the remote.

### 7.7 Defaults that silently do the wrong thing
MPNN has no design scope by default and redesigns *every* residue. On a
scaffold redesign it returned sequences 38% divergent from the parent,
mutating a conserved pocket residue in all 128 sequences — while the job
succeeded, the log was clean and the QC summary looked fine. Read the defaults
of every stage you chain; the dangerous ones do not error.

### 7.8 Metrics that describe the input, not the output
A QC count of chainbreaks reported 0/16 designs "clean" because the crystal
template had unmodelled loops that every design inherited. Subtract the input's
baseline, or the metric measures your template.

### 7.9 Counting shared resources twice
A node usually belongs to several partitions, so summing per-partition GPU
counts overstates what is free. Deduplicate by node before routing on it.

### 7.10 Reserved partitions look idle
They sit empty precisely because few people may use them, so they read as the
best place to send a job. Filter by `AllowAccounts` (§3).

### 7.11 Cluster-specific job scripts
A generated `job.sh` bakes in a module, venv and partition. Moving a job to
another cluster means **regenerating**, not resubmitting.

### 7.12 `$TMPDIR` disappears
It is deleted when the job ends, taking any diagnostic written there. Copy
anything you will want to read back to shared storage before exiting.

### 7.13 Relative paths resolve against something you did not expect
RFD3 resolves a relative `input` against the directory of the *inputs JSON*,
not the job's cwd. Every design died in the validator until both write sites
shared one prefix constant.

## 8. Testing without burning cluster time

Almost all of it can be tested locally:

- **Stub the other tool.** Put a fake `hpcjob` earlier on `PATH` that prints an
  old `--help`, and check the generator warns instead of emitting a broken line.
- **Synthesise the report.** Routing logic takes a dict; feed it
  `{"gpu_totals": {...}}` directly and assert it moves up a tier and refuses to
  move down. Live cluster state changes between two calls and cannot test the
  branch you care about — an apparent routing bug during development turned out
  to be a GPU freeing up mid-test.
- **Fabricate outputs.** Gzipped CIFs, sidecar JSON with fake metrics, a
  synthetic `slurm-*.out` — enough to test the log parser, the QC baseline and
  the staging step end to end.

Reserve the cluster for what genuinely needs it: the install, the memory curve,
and one real end-to-end run.

## 9. Checklist for a new tool

- [ ] `hpcjob preflight` shows the cluster up and the GPUs you need
- [ ] Generator validates inputs locally, against the installed schema
- [ ] No cluster name, path, partition or command in Python
- [ ] Helper package shipped into the job dir; stdlib-only; cannot raise
- [ ] Success check matches what the model actually writes
- [ ] `HANDOFF.md` written, install done, reply folded back into all three places
- [ ] Resource model measured; measured vs extrapolated labelled
- [ ] One real end-to-end run, with its bugs fixed
- [ ] Every claim in the docs is true of the *pushed* code
