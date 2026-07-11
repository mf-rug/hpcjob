"""hpcjob — submit SLURM jobs to and pull results from remote HPC clusters via SSH.

A single tool that supersedes the separate `hpc-submit` (upload + sbatch) and
`rsyncer` (find-by-name + download) tools. Multi-cluster: every subcommand takes
`--cluster NAME`, resolved from a shared registry (`~/.config/hpcjob/clusters.yaml`).
"""

__version__ = "0.1.0"
