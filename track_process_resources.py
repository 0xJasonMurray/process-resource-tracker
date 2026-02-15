#!/usr/bin/env python3
"""
Track resource utilization for processes in a systemd service cgroup.

Metrics are computed per PID:
- CPU usage (% of one CPU core) min/max/avg
- RSS memory (bytes) min/max/avg
- Disk I/O throughput (bytes/sec from /proc/<pid>/io read+write) min/max/avg
- Transfer throughput (bytes/sec from /proc/<pid>/io rchar+wchar) min/max/avg

No third-party dependencies are required.
"""

from __future__ import annotations

import argparse
import curses
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple


STOP = False


def _handle_signal(signum, frame):
    del signum, frame
    global STOP
    STOP = True


@dataclass
class MetricAccumulator:
    minimum: float = float("inf")
    maximum: float = float("-inf")
    total: float = 0.0
    count: int = 0

    def add(self, value: Optional[float]) -> None:
        if value is None:
            return
        if value < self.minimum:
            self.minimum = value
        if value > self.maximum:
            self.maximum = value
        self.total += value
        self.count += 1

    def as_tuple(self) -> Tuple[float, float, float]:
        if self.count == 0:
            return (0.0, 0.0, 0.0)
        return (self.minimum, self.maximum, self.total / self.count)


@dataclass
class ProcessStats:
    pid: int
    name: str
    ppid: int
    first_seen: float
    last_seen: float
    cpu: MetricAccumulator = field(default_factory=MetricAccumulator)
    mem: MetricAccumulator = field(default_factory=MetricAccumulator)
    disk: MetricAccumulator = field(default_factory=MetricAccumulator)
    xfer: MetricAccumulator = field(default_factory=MetricAccumulator)
    current_cpu: float = 0.0
    current_mem: float = 0.0
    current_disk: float = 0.0
    current_xfer: float = 0.0


@dataclass
class PrevSample:
    cpu_ticks: int
    disk_bytes: int
    xfer_bytes: int
    timestamp: float


def read_stat(pid: int) -> Optional[Tuple[str, int, int, int]]:
    """Return (name, ppid, total_cpu_ticks, rss_pages)."""
    path = f"/proc/{pid}/stat"
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None

    lpar = raw.find("(")
    rpar = raw.rfind(")")
    if lpar == -1 or rpar == -1 or rpar <= lpar:
        return None

    name = raw[lpar + 1 : rpar]
    tail = raw[rpar + 2 :].split()
    if len(tail) < 22:
        return None

    try:
        ppid = int(tail[1])
        utime = int(tail[11])
        stime = int(tail[12])
        rss_pages = int(tail[21])
    except ValueError:
        return None

    return (name, ppid, utime + stime, rss_pages)


def read_io_counters(pid: int) -> Optional[Tuple[int, int]]:
    path = f"/proc/{pid}/io"
    try:
        with open(path, "r", encoding="utf-8") as f:
            disk_total = 0
            xfer_total = 0
            for line in f:
                if line.startswith("read_bytes:") or line.startswith("write_bytes:"):
                    disk_total += int(line.split()[1])
                elif line.startswith("rchar:") or line.startswith("wchar:"):
                    xfer_total += int(line.split()[1])
            return (disk_total, xfer_total)
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
        return None


def get_service_cgroup(service: str) -> Optional[str]:
    cmd = ["systemctl", "show", "--property", "ControlGroup", "--value", service]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return None

    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    if not value or value == "/":
        return None

    return value


def pids_in_service_cgroup(service: str) -> Set[int]:
    cgroup = get_service_cgroup(service)
    if cgroup is None:
        return set()

    service_path = os.path.join("/sys/fs/cgroup", cgroup.lstrip("/"))
    if not os.path.isdir(service_path):
        return set()

    pids: Set[int] = set()
    for dirpath, _dirnames, filenames in os.walk(service_path):
        if "cgroup.procs" not in filenames:
            continue
        procs_file = os.path.join(dirpath, "cgroup.procs")
        try:
            with open(procs_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit():
                        pids.add(int(line))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue

    return pids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track CPU/memory/disk metrics for all processes in a systemd service cgroup"
    )
    parser.add_argument(
        "--service",
        required=True,
        help="systemd service name to query (example: nessusd.service)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="sampling interval in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="total runtime in seconds; 0 runs until Ctrl+C (default: 0)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="display a top-like real-time interface while sampling",
    )
    return parser.parse_args()


def print_header() -> None:
    print(
        "PID      NAME                 PPID     "
        "CPU%(min/max/avg)          MEM_MB(min/max/avg)      "
        "DISK_Bps(min/max/avg)      XFER_Bps(min/max/avg)"
    )


def fmt_triplet(values: Tuple[float, float, float], precision: int = 2) -> str:
    a, b, c = values
    return f"{a:.{precision}f}/{b:.{precision}f}/{c:.{precision}f}"


def sample_once(
    service: str,
    now: float,
    clk_tck: int,
    page_size: int,
    stats_by_pid: Dict[int, ProcessStats],
    prev_by_pid: Dict[int, PrevSample],
) -> int:
    tracked = pids_in_service_cgroup(service)
    if not tracked:
        return 0

    for pid in tracked:
        stat = read_stat(pid)
        if stat is None:
            continue
        name, ppid, cpu_ticks, rss_pages = stat
        io_counters = read_io_counters(pid)
        disk_total = io_counters[0] if io_counters is not None else None
        xfer_total = io_counters[1] if io_counters is not None else None
        rss_bytes = rss_pages * page_size

        record = stats_by_pid.get(pid)
        if record is None:
            record = ProcessStats(
                pid=pid,
                name=name,
                ppid=ppid,
                first_seen=now,
                last_seen=now,
            )
            stats_by_pid[pid] = record
        else:
            record.last_seen = now

        prev = prev_by_pid.get(pid)
        cpu_pct: Optional[float] = None
        disk_bps: Optional[float] = None
        xfer_bps: Optional[float] = None

        if prev is not None:
            dt = now - prev.timestamp
            if dt > 0:
                cpu_pct = ((cpu_ticks - prev.cpu_ticks) / clk_tck) / dt * 100.0
                if disk_total is not None:
                    disk_bps = (disk_total - prev.disk_bytes) / dt
                if xfer_total is not None:
                    xfer_bps = (xfer_total - prev.xfer_bytes) / dt

        if cpu_pct is not None:
            cpu_pct = max(0.0, cpu_pct)
            record.current_cpu = cpu_pct
        else:
            record.current_cpu = 0.0

        if disk_bps is not None:
            disk_bps = max(0.0, disk_bps)
            record.current_disk = disk_bps
        else:
            record.current_disk = 0.0

        if xfer_bps is not None:
            xfer_bps = max(0.0, xfer_bps)
            record.current_xfer = xfer_bps
        else:
            record.current_xfer = 0.0

        record.current_mem = float(rss_bytes)
        record.cpu.add(cpu_pct)
        record.mem.add(float(rss_bytes))
        record.disk.add(disk_bps)
        record.xfer.add(xfer_bps)

        if disk_total is not None or xfer_total is not None:
            prev_by_pid[pid] = PrevSample(
                cpu_ticks=cpu_ticks,
                disk_bytes=disk_total if disk_total is not None else (prev.disk_bytes if prev else 0),
                xfer_bytes=xfer_total if xfer_total is not None else (prev.xfer_bytes if prev else 0),
                timestamp=now,
            )
        elif prev is None:
            prev_by_pid[pid] = PrevSample(
                cpu_ticks=cpu_ticks,
                disk_bytes=0,
                xfer_bytes=0,
                timestamp=now,
            )
        else:
            prev_by_pid[pid] = PrevSample(
                cpu_ticks=cpu_ticks,
                disk_bytes=prev.disk_bytes,
                xfer_bytes=prev.xfer_bytes,
                timestamp=now,
            )

    return len(tracked)


def render_live(
    stdscr,
    service: str,
    interval: float,
    end_at: Optional[float],
    clk_tck: int,
    page_size: int,
    stats_by_pid: Dict[int, ProcessStats],
    prev_by_pid: Dict[int, PrevSample],
) -> None:
    stdscr.nodelay(True)
    curses.curs_set(0)

    while not STOP:
        now = time.monotonic()
        if end_at is not None and now >= end_at:
            break

        tracked_count = sample_once(service, now, clk_tck, page_size, stats_by_pid, prev_by_pid)

        stdscr.erase()
        height, width = stdscr.getmaxyx()

        status = (
            f"service={service} interval={interval:.2f}s tracked_now={tracked_count} "
            "q=quit Ctrl+C=stop"
        )
        stdscr.addnstr(0, 0, status, max(0, width - 1))

        header = "PID      NAME                 PPID     CPU%      MEM_MB    DISK_Bps    XFER_Bps"
        stdscr.addnstr(1, 0, header, max(0, width - 1))

        rows = sorted(
            stats_by_pid.values(),
            key=lambda rec: (rec.current_cpu, rec.current_mem),
            reverse=True,
        )

        row_idx = 2
        for rec in rows:
            if row_idx >= height - 1:
                break
            line = (
                f"{rec.pid:<8d} {rec.name[:20]:<20} {rec.ppid:<8d} "
                f"{rec.current_cpu:>8.2f} "
                f"{(rec.current_mem / (1024 * 1024)):>9.2f} "
                f"{rec.current_disk:>10.2f} "
                f"{rec.current_xfer:>10.2f}"
            )
            stdscr.addnstr(row_idx, 0, line, max(0, width - 1))
            row_idx += 1

        stdscr.refresh()

        char = stdscr.getch()
        if char in (ord("q"), ord("Q")):
            break

        sleep_left = interval - (time.monotonic() - now)
        if sleep_left > 0:
            time.sleep(sleep_left)


def main() -> int:
    args = parse_args()

    if args.interval <= 0:
        print("--interval must be > 0", file=sys.stderr)
        return 2

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    page_size = os.sysconf("SC_PAGE_SIZE")

    stats_by_pid: Dict[int, ProcessStats] = {}
    prev_by_pid: Dict[int, PrevSample] = {}

    start = time.monotonic()
    end_at = start + args.duration if args.duration > 0 else None

    if args.live:
        curses.wrapper(
            render_live,
            args.service,
            args.interval,
            end_at,
            clk_tck,
            page_size,
            stats_by_pid,
            prev_by_pid,
        )
    else:
        print(f"Tracking service={args.service} interval={args.interval:.2f}s")
        print("Press Ctrl+C to stop." if end_at is None else f"Will stop after {args.duration:.1f}s.")

        while not STOP:
            now = time.monotonic()
            if end_at is not None and now >= end_at:
                break

            _tracked_count = sample_once(
                args.service,
                now,
                clk_tck,
                page_size,
                stats_by_pid,
                prev_by_pid,
            )
            sleep_left = args.interval - (time.monotonic() - now)
            if sleep_left > 0:
                time.sleep(sleep_left)

    if not stats_by_pid:
        print("No matching process data collected.")
        return 1

    print()
    print_header()

    for pid in sorted(stats_by_pid):
        rec = stats_by_pid[pid]
        cpu_min, cpu_max, cpu_avg = rec.cpu.as_tuple()

        mem_min_b, mem_max_b, mem_avg_b = rec.mem.as_tuple()
        mem_triplet = (
            mem_min_b / (1024 * 1024),
            mem_max_b / (1024 * 1024),
            mem_avg_b / (1024 * 1024),
        )

        disk_min, disk_max, disk_avg = rec.disk.as_tuple()
        xfer_min, xfer_max, xfer_avg = rec.xfer.as_tuple()

        print(
            f"{pid:<8d} {rec.name[:20]:<20} {rec.ppid:<8d} "
            f"{fmt_triplet((cpu_min, cpu_max, cpu_avg), 2):<24} "
            f"{fmt_triplet(mem_triplet, 2):<25} "
            f"{fmt_triplet((disk_min, disk_max, disk_avg), 2):<24} "
            f"{fmt_triplet((xfer_min, xfer_max, xfer_avg), 2)}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
