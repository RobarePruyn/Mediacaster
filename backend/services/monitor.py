"""
System resource monitoring — CPU, RAM, network, per-stream ffmpeg breakdown.
Uses psutil for system metrics and per-process stats on ffmpeg PIDs.
"""

import logging
import time
from typing import Dict, Optional
import psutil

logger = logging.getLogger("monitor")

# Cache for network rate calculation (need two samples)
_last_net_sample: Optional[Dict] = None
_last_net_time: float = 0.0


def get_system_stats() -> dict:
    """
    Collect system-wide resource utilization.
    Returns CPU %, memory stats, and network throughput.
    """
    global _last_net_sample, _last_net_time

    # CPU — non-blocking 0.5s sample
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_count = psutil.cpu_count()

    # Memory
    mem = psutil.virtual_memory()
    memory_total_mb = round(mem.total / (1024 * 1024), 1)
    memory_used_mb = round(mem.used / (1024 * 1024), 1)
    memory_percent = mem.percent

    # Network throughput — calculate delta between two samples
    net_counters = psutil.net_io_counters()
    current_time = time.time()

    network_tx_mbps = 0.0
    network_rx_mbps = 0.0

    if _last_net_sample is not None:
        elapsed = current_time - _last_net_time
        if elapsed > 0:
            # Bytes per second → Megabits per second
            tx_bytes_delta = net_counters.bytes_sent - _last_net_sample["bytes_sent"]
            rx_bytes_delta = net_counters.bytes_recv - _last_net_sample["bytes_recv"]
            network_tx_mbps = round((tx_bytes_delta / elapsed) * 8 / 1_000_000, 2)
            network_rx_mbps = round((rx_bytes_delta / elapsed) * 8 / 1_000_000, 2)

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
    Get CPU and memory usage for a specific PID (ffmpeg process).
    Returns zeros if the process doesn't exist or can't be read.
    """
    try:
        proc = psutil.Process(pid)
        # cpu_percent with interval=None uses cached value (non-blocking)
        cpu = proc.cpu_percent(interval=None)
        mem_info = proc.memory_info()
        memory_mb = round(mem_info.rss / (1024 * 1024), 1)

        # Include child processes (ffmpeg may fork)
        for child in proc.children(recursive=True):
            try:
                cpu += child.cpu_percent(interval=None)
                child_mem = child.memory_info()
                memory_mb += round(child_mem.rss / (1024 * 1024), 1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return {"cpu_percent": round(cpu, 1), "memory_mb": memory_mb}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {"cpu_percent": 0.0, "memory_mb": 0.0}


def estimate_additional_streams(
    system_stats: dict,
    active_stream_stats: list,
    max_cpu_percent: float = 80.0,
    max_memory_percent: float = 80.0,
) -> dict:
    """
    Estimate how many additional streams can be supported without degradation.
    Based on average per-stream resource usage and configured ceilings.
    """
    active_count = len(active_stream_stats)

    if active_count == 0:
        # No streams running — estimate based on system capacity
        # Assume each stream uses roughly 3% CPU and 100MB RAM as a baseline
        # (re-mux is very lightweight, but this is a conservative estimate)
        cpu_headroom = max(0, max_cpu_percent - system_stats["cpu_percent"])
        mem_headroom_mb = max(0, (max_memory_percent / 100 * system_stats["memory_total_mb"])
                              - system_stats["memory_used_mb"])
        estimated_by_cpu = int(cpu_headroom / 3.0) if cpu_headroom > 0 else 0
        estimated_by_mem = int(mem_headroom_mb / 100.0) if mem_headroom_mb > 0 else 0
        estimated = min(estimated_by_cpu, estimated_by_mem)
        return {
            "estimated_additional_streams": max(0, estimated),
            "headroom_cpu_percent": round(cpu_headroom, 1),
            "headroom_memory_percent": round(
                max_memory_percent - system_stats["memory_percent"], 1
            ),
        }

    # Calculate average per-stream resource usage from actual measurements
    total_stream_cpu = sum(s.get("cpu_percent", 0) for s in active_stream_stats)
    total_stream_mem = sum(s.get("memory_mb", 0) for s in active_stream_stats)
    avg_cpu_per_stream = total_stream_cpu / active_count if active_count > 0 else 3.0
    avg_mem_per_stream = total_stream_mem / active_count if active_count > 0 else 100.0

    # Avoid division by zero for very lightweight streams
    avg_cpu_per_stream = max(avg_cpu_per_stream, 0.5)
    avg_mem_per_stream = max(avg_mem_per_stream, 10.0)

    # Available headroom
    cpu_headroom = max(0, max_cpu_percent - system_stats["cpu_percent"])
    mem_headroom_mb = max(0, (max_memory_percent / 100 * system_stats["memory_total_mb"])
                          - system_stats["memory_used_mb"])

    estimated_by_cpu = int(cpu_headroom / avg_cpu_per_stream)
    estimated_by_mem = int(mem_headroom_mb / avg_mem_per_stream)
    estimated = min(estimated_by_cpu, estimated_by_mem)

    return {
        "estimated_additional_streams": max(0, estimated),
        "headroom_cpu_percent": round(cpu_headroom, 1),
        "headroom_memory_percent": round(
            max_memory_percent - system_stats["memory_percent"], 1
        ),
    }
