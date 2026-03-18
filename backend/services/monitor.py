"""
System resource monitoring — CPU, RAM, network throughput, and per-stream process stats.

Provides the data behind the Monitoring tab in the frontend UI. Uses psutil for both
system-wide metrics and per-PID resource tracking of ffmpeg playout processes and
browser source containers.

The monitoring endpoint in routes/settings.py calls these functions on each request,
so they're designed to be lightweight and non-blocking. CPU sampling uses interval=None
(returns cached value since last call) and is primed once at module load. Network rates
are calculated from deltas between successive calls.

Key design decisions:
  - Network throughput is calculated as a delta between calls rather than using psutil's
    interval-based measurement, so it doesn't block for a full second.
  - Per-process stats include child processes (ffmpeg may fork, containers have many children).
  - Stream capacity estimation uses actual per-stream averages when streams are running,
    and conservative baselines (3% CPU, 100MB RAM) when idle.
"""

import logging
import time
from typing import Dict, Optional
import psutil

logger = logging.getLogger("monitor")

# Prime the CPU percentage counter at import time. psutil.cpu_percent(interval=None)
# returns 0.0 on the very first call because there's no prior sample to diff against.
# By calling it once here, subsequent calls in get_system_stats() return real values
# without blocking the request thread for 0.5 seconds.
psutil.cpu_percent(interval=None)

# Module-level cache for network rate calculation.
# We need two consecutive samples to compute bytes/second, so we store the
# previous sample's counters and timestamp here between calls.
_last_net_sample: Optional[Dict] = None
_last_net_time: float = 0.0


def get_system_stats() -> dict:
    """
    Collect system-wide resource utilization metrics.

    CPU is sampled with interval=None (non-blocking, returns the delta since the
    last call). The counter is primed at module load time so the first API call
    gets a real value instead of 0.0.

    Network throughput is computed as the delta in bytes since the last call,
    converted to megabits per second. The first call after startup will return 0.0
    since there's no previous sample to diff against.

    Returns:
        Dict with keys:
          - cpu_percent: System-wide CPU usage (0-100)
          - cpu_count: Number of logical CPU cores
          - memory_total_mb: Total physical RAM in MB
          - memory_used_mb: Used RAM in MB
          - memory_percent: RAM usage percentage (0-100)
          - network_tx_mbps: Outbound network throughput in Mbps
          - network_rx_mbps: Inbound network throughput in Mbps
    """
    global _last_net_sample, _last_net_time

    # CPU — non-blocking call returns delta since last invocation (primed at import)
    cpu_percent = psutil.cpu_percent(interval=None)
    cpu_count = psutil.cpu_count()

    # Memory — straightforward snapshot from the OS
    mem = psutil.virtual_memory()
    memory_total_mb = round(mem.total / (1024 * 1024), 1)
    memory_used_mb = round(mem.used / (1024 * 1024), 1)
    memory_percent = mem.percent

    # Network throughput — computed as delta between consecutive calls
    net_counters = psutil.net_io_counters()
    current_time = time.time()

    network_tx_mbps = 0.0
    network_rx_mbps = 0.0

    if _last_net_sample is not None:
        elapsed = current_time - _last_net_time
        if elapsed > 0:
            # Convert byte delta to megabits: (bytes / seconds) * 8 bits/byte / 1,000,000
            tx_bytes_delta = net_counters.bytes_sent - _last_net_sample["bytes_sent"]
            rx_bytes_delta = net_counters.bytes_recv - _last_net_sample["bytes_recv"]
            network_tx_mbps = round((tx_bytes_delta / elapsed) * 8 / 1_000_000, 2)
            network_rx_mbps = round((rx_bytes_delta / elapsed) * 8 / 1_000_000, 2)

    # Store current counters for next call's delta calculation
    _last_net_sample = {
        "bytes_sent": net_counters.bytes_sent,
        "bytes_recv": net_counters.bytes_recv,
    }
    _last_net_time = current_time

    return {
        "cpu_percent": cpu_percent,
        "cpu_count": cpu_count,
        "memory_total_mb": memory_total_mb,
        "memory_used_mb": memory_used_mb,
        "memory_percent": memory_percent,
        "network_tx_mbps": network_tx_mbps,
        "network_rx_mbps": network_rx_mbps,
    }


def get_process_stats(pid: int) -> dict:
    """
    Get CPU and memory usage for a specific PID and all its child processes.

    This is used to measure per-stream resource consumption. For playlist streams,
    the PID is the ffmpeg process. For browser sources, it's the container's init
    process — we recursively include children (Firefox, x11vnc, ffmpeg, etc.) to
    get the total resource footprint.

    Uses cpu_percent(interval=None) which returns a cached/instantaneous value
    rather than blocking. This means the first call for a new PID may return 0.0;
    subsequent calls will be accurate.

    Args:
        pid: Process ID to measure

    Returns:
        Dict with cpu_percent (float) and memory_mb (float), both 0.0 if the
        process doesn't exist or can't be accessed.
    """
    try:
        proc = psutil.Process(pid)
        # interval=None returns the CPU usage since the last call (non-blocking).
        # First call for a PID returns 0.0 — subsequent calls are accurate.
        cpu = proc.cpu_percent(interval=None)
        mem_info = proc.memory_info()
        # RSS (Resident Set Size) is the actual physical memory used by the process
        memory_mb = round(mem_info.rss / (1024 * 1024), 1)

        # Include all child processes — important for browser sources where the
        # container's init PID has many children (Firefox, ffmpeg, x11vnc, etc.)
        for child in proc.children(recursive=True):
            try:
                cpu += child.cpu_percent(interval=None)
                child_mem = child.memory_info()
                memory_mb += round(child_mem.rss / (1024 * 1024), 1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # Child may have exited between listing and measuring — safe to skip
                pass

        return {"cpu_percent": round(cpu, 1), "memory_mb": memory_mb}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        # Process gone or we lack permissions — return zeroes rather than erroring
        return {"cpu_percent": 0.0, "memory_mb": 0.0}


def estimate_additional_streams(
    system_stats: dict,
    active_stream_stats: list,
    max_cpu_percent: float = 80.0,
    max_memory_percent: float = 80.0,
    max_bandwidth_percent: float = 80.0,
    link_speed_mbps: float = 1000.0,
) -> dict:
    """
    Estimate how many additional streams the server can support without degradation.

    Uses three resource dimensions — CPU, memory, and network bandwidth — and
    returns the most constrained estimate (whichever resource runs out first).

    The estimation strategy depends on whether streams are currently running:

    With active streams: Uses the measured average per-stream CPU, memory, and
    bandwidth usage to project how many more can fit within the configured ceilings.

    Without active streams: Falls back to conservative baselines (3% CPU, 100MB RAM,
    10 Mbps TX per stream). Playlist streams (remux only) are much lighter than this,
    but browser sources can be heavier, so this is a safe middle ground.

    Args:
        system_stats: Dict from get_system_stats() with current system metrics
        active_stream_stats: List of dicts from get_process_stats() for each running stream
        max_cpu_percent: CPU utilization ceiling (default 80%) — leave headroom for the OS
        max_memory_percent: Memory utilization ceiling (default 80%)
        max_bandwidth_percent: TX bandwidth utilization ceiling (default 80%)
        link_speed_mbps: NIC link speed in Mbps (default 1000 for 1 Gbps)

    Returns:
        Dict with:
          - estimated_additional_streams: Conservative estimate (min of all three dimensions)
          - headroom_cpu_percent: Remaining CPU headroom before hitting the ceiling
          - headroom_memory_percent: Remaining memory headroom before hitting the ceiling
          - headroom_bandwidth_percent: Remaining TX bandwidth headroom before hitting the ceiling
    """
    active_count = len(active_stream_stats)

    # Current TX bandwidth as a percentage of link speed
    current_bw_percent = (
        (system_stats["network_tx_mbps"] / link_speed_mbps * 100)
        if link_speed_mbps > 0 else 0.0
    )
    bw_headroom = max(0, max_bandwidth_percent - current_bw_percent)

    if active_count == 0:
        # No streams running — use conservative baseline estimates.
        # 3% CPU, 100MB RAM, 10 Mbps TX per stream is a safe middle ground between
        # lightweight remux streams and heavier browser sources.
        cpu_headroom = max(0, max_cpu_percent - system_stats["cpu_percent"])
        mem_headroom_mb = max(0, (max_memory_percent / 100 * system_stats["memory_total_mb"])
                              - system_stats["memory_used_mb"])
        # 10 Mbps baseline per stream (~8 Mbps video + overhead)
        bw_headroom_mbps = bw_headroom / 100 * link_speed_mbps
        estimated_by_cpu = int(cpu_headroom / 3.0) if cpu_headroom > 0 else 0
        estimated_by_mem = int(mem_headroom_mb / 100.0) if mem_headroom_mb > 0 else 0
        estimated_by_bw = int(bw_headroom_mbps / 10.0) if bw_headroom_mbps > 0 else 0
        # Take the minimum — any resource can be the bottleneck
        estimated = min(estimated_by_cpu, estimated_by_mem, estimated_by_bw)
        return {
            "estimated_additional_streams": max(0, estimated),
            "headroom_cpu_percent": round(cpu_headroom, 1),
            "headroom_memory_percent": round(
                max_memory_percent - system_stats["memory_percent"], 1
            ),
            "headroom_bandwidth_percent": round(bw_headroom, 1),
        }

    # Calculate average per-stream resource usage from actual measurements
    total_stream_cpu = sum(s.get("cpu_percent", 0) for s in active_stream_stats)
    total_stream_mem = sum(s.get("memory_mb", 0) for s in active_stream_stats)
    avg_cpu_per_stream = total_stream_cpu / active_count if active_count > 0 else 3.0
    avg_mem_per_stream = total_stream_mem / active_count if active_count > 0 else 100.0

    # Estimate per-stream TX bandwidth from total measured TX divided by active count.
    # This is an approximation — it includes non-stream TX (API, etc.) but for a
    # dedicated streaming server, multicast output dominates the TX budget.
    avg_bw_per_stream = system_stats["network_tx_mbps"] / active_count if active_count > 0 else 10.0

    # Floor values to prevent division-by-zero for extremely lightweight streams
    avg_cpu_per_stream = max(avg_cpu_per_stream, 0.5)
    avg_mem_per_stream = max(avg_mem_per_stream, 10.0)
    avg_bw_per_stream = max(avg_bw_per_stream, 1.0)  # At least 1 Mbps per stream

    # Available headroom between current usage and configured ceilings
    cpu_headroom = max(0, max_cpu_percent - system_stats["cpu_percent"])
    mem_headroom_mb = max(0, (max_memory_percent / 100 * system_stats["memory_total_mb"])
                          - system_stats["memory_used_mb"])
    bw_headroom_mbps = bw_headroom / 100 * link_speed_mbps

    # Project how many more streams fit in the remaining headroom
    estimated_by_cpu = int(cpu_headroom / avg_cpu_per_stream)
    estimated_by_mem = int(mem_headroom_mb / avg_mem_per_stream)
    estimated_by_bw = int(bw_headroom_mbps / avg_bw_per_stream)
    estimated = min(estimated_by_cpu, estimated_by_mem, estimated_by_bw)

    return {
        "estimated_additional_streams": max(0, estimated),
        "headroom_cpu_percent": round(cpu_headroom, 1),
        "headroom_memory_percent": round(
            max_memory_percent - system_stats["memory_percent"], 1
        ),
        "headroom_bandwidth_percent": round(bw_headroom, 1),
    }
