/**
 * Stream management panel — handles both playlist and browser source types.
 */
import React, { useState, useEffect, useCallback } from 'react';
import {
  createStream, updateStream, deleteStream, updateBrowserConfig,
  removePlaylistItem, reorderPlaylist,
  startStream, stopStream, restartStream, getStreamStatus,
} from '../api';

export default function StreamPanel({ streams, selectedStreamId, onSelectStream, assets, isLoading, onRefresh, isAdmin }) {
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ name: '', multicast_address: '', multicast_port: '', playback_mode: 'loop' });
  const [creating, setCreating] = useState(false);
  const [newSourceType, setNewSourceType] = useState('playlist');
  const [liveStatus, setLiveStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');

  // Browser config state
  const [browserUrl, setBrowserUrl] = useState('');
  const [browserAudio, setBrowserAudio] = useState(false);
  const [editingBrowser, setEditingBrowser] = useState(false);

  const currentStream = streams.find(s => s.id === selectedStreamId);
  const isBrowser = currentStream?.source_type === 'browser';

  const pollStatus = useCallback(async () => {
    if (!selectedStreamId) return;
    try { setLiveStatus(await getStreamStatus(selectedStreamId)); } catch {}
  }, [selectedStreamId]);

  useEffect(() => {
    pollStatus();
    const interval = setInterval(pollStatus, 2000);
    return () => clearInterval(interval);
  }, [pollStatus]);

  useEffect(() => {
    if (currentStream) {
      setForm({
        name: currentStream.name,
        multicast_address: currentStream.multicast_address,
        multicast_port: String(currentStream.multicast_port),
        playback_mode: currentStream.playback_mode,
      });
      if (currentStream.browser_source) {
        setBrowserUrl(currentStream.browser_source.url || '');
        setBrowserAudio(currentStream.browser_source.capture_audio || false);
      }
    }
  }, [currentStream]);

  // --- CRUD ---
  const handleCreate = async () => {
    setCreating(true); setErrorMsg('');
    try {
      const newStream = await createStream({
        name: newSourceType === 'browser' ? 'Browser Source' : 'New Stream',
        multicast_address: '239.1.1.1', multicast_port: 5000,
        playback_mode: 'loop', source_type: newSourceType,
      });
      await onRefresh();
      onSelectStream(newStream.id);
      setNewSourceType('playlist');
    } catch (e) { setErrorMsg(e.message); }
    finally { setCreating(false); }
  };

  const handleSave = async () => {
    if (!selectedStreamId) return; setErrorMsg('');
    try {
      await updateStream(selectedStreamId, { ...form, multicast_port: parseInt(form.multicast_port, 10) });
      setEditing(false); onRefresh();
    } catch (e) { setErrorMsg(e.message); }
  };

  const handleSaveBrowser = async () => {
    if (!selectedStreamId) return; setErrorMsg('');
    try {
      await updateBrowserConfig(selectedStreamId, browserUrl, browserAudio);
      setEditingBrowser(false); onRefresh();
    } catch (e) { setErrorMsg(e.message); }
  };

  const handleDelete = async () => {
    if (!window.confirm('Delete this stream?')) return;
    try { await deleteStream(selectedStreamId); onSelectStream(null); onRefresh(); }
    catch (e) { setErrorMsg(e.message); }
  };

  // --- Playlist ---
  const handleRemoveItem = async (itemId) => {
    try { await removePlaylistItem(selectedStreamId, itemId); onRefresh(); }
    catch (e) { setErrorMsg(e.message); }
  };

  const handleMoveItem = async (idx, dir) => {
    if (!currentStream) return;
    const items = [...currentStream.items];
    const target = idx + dir;
    if (target < 0 || target >= items.length) return;
    [items[idx], items[target]] = [items[target], items[idx]];
    try { await reorderPlaylist(selectedStreamId, items.map(i => i.asset_id)); onRefresh(); }
    catch (e) { setErrorMsg(e.message); }
  };

  // --- Transport ---
  const doAction = async (fn) => {
    setBusy(true); setErrorMsg('');
    try { await fn(); onRefresh(); } catch (e) { setErrorMsg(e.message); }
    finally { setBusy(false); }
  };

  const isRunning = currentStream?.status === 'running' || currentStream?.status === 'starting';
  const hasReadyItems = currentStream?.items?.some(i => i.asset.status === 'ready');
  const canStart = isBrowser
    ? (currentStream?.browser_source?.url && currentStream.browser_source.url !== 'about:blank')
    : hasReadyItems;

  // noVNC URL — proxied through nginx at /novnc/{port}/
  const novncPort = currentStream?.browser_source?.novnc_port;
  const serverHost = window.location.hostname;
  const novncUrl = (novncPort && isRunning)
    ? `http://${serverHost}:${novncPort}/vnc_lite.html?autoconnect=true&resize=scale&reconnect=true&scaleViewport=true&show_dot=true&scaleViewport=true&show_dot=true`
    : null;

  if (isLoading) {
    return (<div className="stream-panel"><div className="panel-header"><h2>Stream Output</h2></div>
      <div className="loading-state">Loading...</div></div>);
  }

  return (
    <div className="stream-panel">
      <div className="panel-header">
        <h2>Stream Output</h2>
        {isAdmin && (
          <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
            <select className="source-type-select" value={newSourceType}
              onChange={e => setNewSourceType(e.target.value)}>
              <option value="playlist">Playlist</option>
              <option value="browser">Browser</option>
            </select>
            <button className="btn btn-sm btn-accent" onClick={handleCreate} disabled={creating}>
              + New
            </button>
          </div>
        )}
      </div>

      {/* Stream tabs */}
      {streams.length > 0 && (
        <div className="stream-tabs">
          {streams.map(s => (
            <button key={s.id}
              className={`stream-tab ${s.id === selectedStreamId ? 'active' : ''}`}
              onClick={() => onSelectStream(s.id)}>
              <span className={`status-dot status-${s.status}`} />
              {s.source_type === 'browser' ? '🌐 ' : ''}{s.name}
            </button>
          ))}
        </div>
      )}

      {errorMsg && <div className="panel-error">{errorMsg}</div>}

      {!currentStream ? (
        <div className="empty-state">
          {streams.length === 0
            ? (isAdmin ? 'No streams configured. Create one to get started.'
                       : 'No channels assigned to you. Ask an administrator.')
            : 'Select a stream above.'}
        </div>
      ) : (<>
        {/* Multicast config */}
        <div className="config-section">
          <div className="config-header">
            <h3>Multicast Output</h3>
            {isAdmin && (
              !editing
                ? <button className="btn btn-sm btn-ghost" onClick={() => setEditing(true)}>Edit</button>
                : <div className="config-actions">
                    <button className="btn btn-sm btn-accent" onClick={handleSave}>Save</button>
                    <button className="btn btn-sm btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
                  </div>
            )}
          </div>
          {editing ? (
            <div className="config-form">
              <div className="form-row">
                <div className="form-group"><label>Stream Name</label>
                  <input type="text" value={form.name} onChange={e => setForm({...form, name: e.target.value})} /></div>
              </div>
              <div className="form-row">
                <div className="form-group"><label>Multicast Address</label>
                  <input type="text" value={form.multicast_address} onChange={e => setForm({...form, multicast_address: e.target.value})} /></div>
                <div className="form-group form-group-sm"><label>Port</label>
                  <input type="number" value={form.multicast_port} onChange={e => setForm({...form, multicast_port: e.target.value})} min="1024" max="65535" /></div>
              </div>
              {!isBrowser && (
                <div className="form-row">
                  <div className="form-group"><label>Playback Mode</label>
                    <select value={form.playback_mode} onChange={e => setForm({...form, playback_mode: e.target.value})}>
                      <option value="loop">Loop Continuously</option>
                      <option value="oneshot">Play Once</option>
                    </select></div>
                </div>
              )}
            </div>
          ) : (
            <div className="config-display">
              <div className="config-value"><span className="config-label">Type</span>
                <span className={`badge badge-sm ${isBrowser ? 'badge-info' : 'badge-success'}`}>
                  {isBrowser ? 'Browser Source' : 'Playlist'}
                </span></div>
              <div className="config-value"><span className="config-label">Destination</span>
                <span className="mono config-addr">udp://{currentStream.multicast_address}:{currentStream.multicast_port}</span></div>
              {!isBrowser && (
                <div className="config-value"><span className="config-label">Mode</span>
                  <span>{currentStream.playback_mode === 'loop' ? 'Loop' : 'One-shot'}</span></div>
              )}
            </div>
          )}
        </div>

        {/* Browser source config (if browser type) */}
        {isBrowser && (
          <div className="config-section">
            <div className="config-header">
              <h3>Browser Source</h3>
              {isAdmin && (
                !editingBrowser
                  ? <button className="btn btn-sm btn-ghost" onClick={() => setEditingBrowser(true)}>Edit</button>
                  : <div className="config-actions">
                      <button className="btn btn-sm btn-accent" onClick={handleSaveBrowser}>Save</button>
                      <button className="btn btn-sm btn-ghost" onClick={() => setEditingBrowser(false)}>Cancel</button>
                    </div>
              )}
            </div>
            {editingBrowser ? (
              <div className="config-form">
                <div className="form-row">
                  <div className="form-group"><label>URL</label>
                    <input type="text" value={browserUrl} onChange={e => setBrowserUrl(e.target.value)}
                      placeholder="https://example.com" /></div>
                </div>
                <div className="form-row">
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13,
                    color: 'var(--text-secondary)', padding: '4px 0' }}>
                    <input type="checkbox" checked={browserAudio}
                      onChange={e => setBrowserAudio(e.target.checked)} />
                    Capture browser audio
                  </label>
                </div>
              </div>
            ) : (
              <div className="config-display">
                <div className="config-value"><span className="config-label">URL</span>
                  <span className="mono" style={{ fontSize: 12, wordBreak: 'break-all' }}>
                    {currentStream.browser_source?.url || 'Not configured'}
                  </span></div>
                <div className="config-value"><span className="config-label">Audio</span>
                  <span>{currentStream.browser_source?.capture_audio ? 'Capturing' : 'Disabled'}</span></div>
              </div>
            )}
          </div>
        )}

        {/* noVNC interactive view (when browser source is running) */}
        {isBrowser && isRunning && novncPort && (
          <div className="browser-preview">
            <div className="config-header"><h3>Live Browser View (interactive)</h3></div>
            <iframe
              src={novncUrl}
              title="Browser Source"
              className="novnc-frame"
              allow="clipboard-read; clipboard-write"
            />
          </div>
        )}

        {/* Transport controls */}
        <div className="transport-section">
          <div className="transport-status">
            <span className={`status-indicator status-${currentStream.status}`}>
              {currentStream.status.toUpperCase()}
            </span>
            {liveStatus?.runtime?.ffmpeg_pid && (
              <span className="mono pid-display">PID {liveStatus.runtime.ffmpeg_pid}</span>
            )}
            {liveStatus?.runtime?.pid && !liveStatus?.runtime?.ffmpeg_pid && (
              <span className="mono pid-display">PID {liveStatus.runtime.pid}</span>
            )}
          </div>
          <div className="transport-controls">
            {isRunning ? (<>
              <button className="btn btn-danger" disabled={busy}
                onClick={() => doAction(() => stopStream(selectedStreamId))}>■ Stop</button>
              <button className="btn btn-warning" disabled={busy}
                onClick={() => doAction(() => restartStream(selectedStreamId))}>↻ Restart</button>
            </>) : (
              <button className="btn btn-success" disabled={busy || !canStart}
                onClick={() => doAction(() => startStream(selectedStreamId))}
                title={!canStart ? (isBrowser ? 'Configure a URL first' : 'Add ready assets first') : ''}>
                ▶ Start
              </button>
            )}
            {isAdmin && (
              <button className="btn btn-ghost btn-delete" onClick={handleDelete}>🗑</button>
            )}
          </div>
        </div>

        {/* Playlist (only for playlist source type) */}
        {!isBrowser && (
          <div className="playlist-section">
            <h3>Playlist ({currentStream.items.length} items)</h3>
            {currentStream.items.length === 0 ? (
              <div className="empty-state">
                Empty playlist. Use the "+ Stream" button on library assets.
              </div>
            ) : (
              <div className="playlist">
                {currentStream.items.map((item, idx) => (
                  <div key={item.id} className="playlist-item">
                    <span className="playlist-position mono">{idx + 1}</span>
                    <div className="playlist-thumb">
                      {item.asset.thumbnail_url
                        ? <img src={item.asset.thumbnail_url} alt="" />
                        : <div className="thumb-placeholder">—</div>}
                    </div>
                    <div className="playlist-info">
                      <span className="playlist-name">{item.asset.display_name}</span>
                      <span className={`badge badge-sm ${
                        item.asset.status === 'ready' ? 'badge-success' : 'badge-warning'
                      }`}>{item.asset.status}</span>
                    </div>
                    <div className="playlist-actions">
                      <button className="btn btn-xs btn-ghost" disabled={idx === 0}
                        onClick={() => handleMoveItem(idx, -1)}>▲</button>
                      <button className="btn btn-xs btn-ghost"
                        disabled={idx === currentStream.items.length - 1}
                        onClick={() => handleMoveItem(idx, 1)}>▼</button>
                      <button className="btn btn-xs btn-ghost btn-delete"
                        onClick={() => handleRemoveItem(item.id)}>✕</button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </>)}
    </div>
  );
}
