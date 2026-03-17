/**
 * Resource monitoring dashboard — CPU, RAM, network, per-stream breakdown,
 * and stream capacity estimation.
 */
import React, { useState, useEffect, useCallback } from 'react';
import { getMonitoring } from '../api';

function BarMeter({ value, max, label, unit, color }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
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
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  const poll = useCallback(async () => {
    try {
      const d = await getMonitoring();
      setData(d);
      setError('');
    } catch (err) { setError(err.message); }
  }, []);

  useEffect(() => {
    poll();
    const interval = setInterval(poll, 2000);
    return () => clearInterval(interval);
  }, [poll]);

  if (error) return <div className="panel-error">{error}</div>;
  if (!data) return <div className="loading-state">Loading monitoring data...</div>;

  const runningStreams = data.active_streams.filter(s => s.status === 'running');
  const totalStreamCpu = runningStreams.reduce((sum, s) => sum + s.cpu_percent, 0);
  const totalStreamMem = runningStreams.reduce((sum, s) => sum + s.memory_mb, 0);

  return (
    <div className="monitoring-panel">
      {/* System overview */}
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

      {/* Capacity estimation */}
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
        </div>
      </div>

      {/* Per-stream breakdown */}
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
