/**
 * Monitoring.jsx — Real-time system resource monitoring dashboard (admin-only).
 *
 * Displays four categories of information:
 *   1. System Resources: CPU usage, memory usage, network TX/RX — rendered as bar meters
 *      with color thresholds (green < 60%, yellow < 80%, red >= 80%).
 *   2. Stream Capacity: active stream count, estimated additional streams the server can
 *      handle, and CPU/memory headroom percentages.
 *   3. Per-Stream Resources: individual CPU and memory usage for each running stream,
 *      plus a totals row. Non-running streams show their status badge instead of metrics.
 *
 * Polls the /monitoring endpoint every 2 seconds for near-real-time updates.
 * The backend (services/monitor.py) uses psutil to gather per-PID stats for each
 * stream's ffmpeg process and aggregates system-wide metrics.
 */
import React, { useState, useEffect, useCallback } from 'react';
import { getMonitoring } from '../api';

/**
 * BarMeter — Reusable horizontal bar gauge component.
 *
 * Renders a labeled progress bar with automatic color transitions:
 *   - Green (or custom color) below 60% utilization
 *   - Yellow/warning between 60-80%
 *   - Red/error above 80%
 *
 * Used for CPU, memory, and network metrics in the system overview section.
 *
 * @param {number} value - Current value to display
 * @param {number} max - Maximum value (defines the 100% bar width)
 * @param {string} label - Text label shown above the bar (e.g., "CPU (4 cores)")
 * @param {string} unit - Unit suffix appended to the value (e.g., "%", " Mbps")
 * @param {string} [color] - Optional override for the bar color when under 60%
 */
function BarMeter({ value, max, label, unit, color }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  // Threshold-based color: red above 80%, yellow above 60%, otherwise the provided color or accent
  const barColor = pct > 80 ? 'var(--status-error)' :
                   pct > 60 ? 'var(--status-warning)' : (color || 'var(--accent)');
  return (
    <div className="meter">
      <div className="meter-header">
        <span className="meter-label">{label}</span>
        <span className="meter-value mono">{value}{unit}</span>
      </div>
      <div className="meter-track">
        <div className="meter-fill" style={{ width: `${pct}%`, background: barColor }} />
      </div>
    </div>
  );
}

export default function Monitoring() {
  /** Full monitoring data payload from the /monitoring endpoint */
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  /**
   * Fetches monitoring data from the backend.
   * Clears any previous error on success so the error banner disappears
   * once connectivity is restored.
   */
  const poll = useCallback(async () => {
    try {
      const d = await getMonitoring();
      setData(d);
      setError('');
    } catch (err) { setError(err.message); }
  }, []);

  // Poll every 2 seconds for near-real-time monitoring updates
  useEffect(() => {
    poll();
    const interval = setInterval(poll, 2000);
    return () => clearInterval(interval);
  }, [poll]);

  if (error) return <div className="panel-error">{error}</div>;
  if (!data) return <div className="loading-state">Loading monitoring data...</div>;

  // Calculate aggregate resource usage across all running streams for the totals row
  const runningStreams = data.active_streams.filter(s => s.status === 'running');
  const totalStreamCpu = runningStreams.reduce((sum, s) => sum + s.cpu_percent, 0);
  const totalStreamMem = runningStreams.reduce((sum, s) => sum + s.memory_mb, 0);

  return (
    <div className="monitoring-panel">
      {/* ── System resource overview (bar meters) ────────────────────────── */}
      <div className="monitor-section">
        <h3 className="monitor-section-title">System Resources</h3>
        <div className="meter-grid">
          <BarMeter
            value={data.cpu_percent} max={100}
            label={`CPU (${data.cpu_count} cores)`} unit="%"
          />
          <BarMeter
            value={data.memory_used_mb} max={data.memory_total_mb}
            label="Memory" unit={` / ${data.memory_total_mb} MB`}
          />
          {/* Network meters use 1000 Mbps as max (1 Gbps reference line) */}
          <BarMeter
            value={data.network_tx_mbps} max={1000}
            label="Network TX" unit=" Mbps"
            color="var(--status-success)"
          />
          <BarMeter
            value={data.network_rx_mbps} max={1000}
            label="Network RX" unit=" Mbps"
            color="var(--status-info)"
          />
        </div>
      </div>

      {/*
        Stream capacity estimation cards.
        The backend calculates estimated_additional_streams based on current per-stream
        resource averages and the configured max_cpu_utilization / max_bandwidth limits.
      */}
      <div className="monitor-section">
        <h3 className="monitor-section-title">Stream Capacity</h3>
        <div className="capacity-grid">
          <div className="capacity-card">
            <span className="capacity-number">{runningStreams.length}</span>
            <span className="capacity-label">Active Streams</span>
          </div>
          <div className="capacity-card">
            <span className="capacity-number capacity-good">
              +{data.estimated_additional_streams}
            </span>
            <span className="capacity-label">Estimated Additional</span>
          </div>
          <div className="capacity-card">
            <span className="capacity-number">{data.headroom_cpu_percent}%</span>
            <span className="capacity-label">CPU Headroom</span>
          </div>
          <div className="capacity-card">
            <span className="capacity-number">{data.headroom_memory_percent}%</span>
            <span className="capacity-label">Memory Headroom</span>
          </div>
          <div className="capacity-card">
            <span className="capacity-number">{data.headroom_bandwidth_percent}%</span>
            <span className="capacity-label">Bandwidth Headroom</span>
          </div>
        </div>
      </div>

      {/* ── Per-stream resource breakdown ────────────────────────────────── */}
      <div className="monitor-section">
        <h3 className="monitor-section-title">
          Per-Stream Resources ({data.active_streams.length} configured)
        </h3>
        {data.active_streams.length === 0 ? (
          <div className="empty-state">No streams configured</div>
        ) : (
          <div className="stream-stats-list">
            {data.active_streams.map((stream) => (
              <div key={stream.stream_id} className="stream-stat-row">
                <div className="stream-stat-info">
                  <span className={`status-dot status-${stream.status}`} />
                  <span className="stream-stat-name">{stream.stream_name}</span>
                  {stream.pid && <span className="mono pid-display">PID {stream.pid}</span>}
                </div>
                {/* Running streams show CPU/RAM metrics; stopped/starting streams show a status badge */}
                {stream.status === 'running' ? (
                  <div className="stream-stat-metrics">
                    <span className="stream-stat-metric">
                      <span className="metric-label">CPU</span>
                      <span className="mono">{stream.cpu_percent}%</span>
                    </span>
                    <span className="stream-stat-metric">
                      <span className="metric-label">RAM</span>
                      <span className="mono">{stream.memory_mb} MB</span>
                    </span>
                  </div>
                ) : (
                  <span className={`badge badge-sm ${
                    stream.status === 'stopped' ? 'badge-info' : 'badge-warning'
                  }`}>{stream.status}</span>
                )}
              </div>
            ))}
            {/* Totals row — only shown when at least one stream is running */}
            {runningStreams.length > 0 && (
              <div className="stream-stat-row stream-stat-total">
                <div className="stream-stat-info">
                  <span className="stream-stat-name">Total (all streams)</span>
                </div>
                <div className="stream-stat-metrics">
                  <span className="stream-stat-metric">
                    <span className="metric-label">CPU</span>
                    <span className="mono">{totalStreamCpu.toFixed(1)}%</span>
                  </span>
                  <span className="stream-stat-metric">
                    <span className="metric-label">RAM</span>
                    <span className="mono">{totalStreamMem.toFixed(0)} MB</span>
                  </span>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
