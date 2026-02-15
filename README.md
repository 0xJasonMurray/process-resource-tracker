# Process Resource Tracker

`track_process_resources.py` monitors all processes that belong to a given `systemd` service cgroup and reports per-process resource usage over time.

It is designed for Linux hosts and reads data directly from `/proc` and `/sys/fs/cgroup`, so it has no third-party Python dependencies.

This project was created because there was no good system for tracking a process and all children and sub-children over time.

## What It Tracks

For each PID discovered under the service cgroup, the script tracks:

- CPU usage percent (`CPU%`) as `% of one CPU core`
- Memory (`RSS`) in MB
- Disk throughput in bytes/sec from `/proc/<pid>/io`:
  - `read_bytes + write_bytes`
- Transfer throughput in bytes/sec from `/proc/<pid>/io`:
  - `rchar + wchar`

For each metric, it calculates `min/max/avg` over the full sampling window.

## How Process Discovery Works

1. Reads the service control group using:
   - `systemctl show --property ControlGroup --value <service>`
2. Walks the corresponding cgroup path under `/sys/fs/cgroup/...`
3. Reads `cgroup.procs` files to build the PID set
4. Samples each PID at the configured interval

Processes can appear/disappear during runtime. Metrics are accumulated for any process seen during the run.

## Requirements

- Linux with `systemd`
- `/proc` and cgroup filesystem available
- Python 3.13+ (per `pyproject.toml`)
- Permission to read target process info from:
  - `/proc/<pid>/stat`
  - `/proc/<pid>/io`

If permission is denied for `/proc/<pid>/io`, CPU/memory can still be sampled for that process, but disk/transfer values may be limited.

## Usage

Run directly:

```bash
./track_process_resources.py --service <name>.service [options]
```

Or via Python:

```bash
python3 track_process_resources.py --service <name>.service [options]
```

### CLI Options

- `--service` (required): systemd unit name (example: `nessusd.service`)
- `--interval` (default `0.5`): sampling interval in seconds, must be `> 0`
- `--duration` (default `0`): total runtime in seconds
  - `0` means run until interrupted (`Ctrl+C`)
- `--live`: show a live top-like curses UI during sampling

## Examples

Sample for 30 seconds every 1 second, then print summary:

```bash
./track_process_resources.py --service ssh.service --interval 1 --duration 30
```

Run continuously until interrupted:

```bash
./track_process_resources.py --service docker.service
```

Run with live view:

```bash
./track_process_resources.py --service nginx.service --live
```

Live mode controls:

- `q`: quit
- `Ctrl+C`: stop

## Output Format

At the end of collection, the script prints a table like:

- `PID`, `NAME`, `PPID`
- `CPU%(min/max/avg)`
- `MEM_MB(min/max/avg)`
- `DISK_Bps(min/max/avg)`
- `XFER_Bps(min/max/avg)`

Notes:

- CPU is computed from deltas in process CPU ticks between samples.
- Disk/XFER throughput is computed from counter deltas per second.
- The first sample for a PID usually contributes memory but not rate-based metrics (CPU/disk/xfer), because a prior sample is required to compute deltas.

## Exit Codes

- `0`: success with collected data
- `1`: no matching process data was collected
- `2`: invalid CLI input (currently invalid `--interval`)

## Troubleshooting

- `No matching process data collected.`
  - Verify service name: `systemctl status <service>`
  - Ensure the service is running and has active processes
  - Confirm cgroup path exists under `/sys/fs/cgroup`
- `--interval must be > 0`
  - Provide a positive interval, e.g. `--interval 0.5`
- Missing disk/transfer metrics
  - Check permissions for reading `/proc/<pid>/io`
