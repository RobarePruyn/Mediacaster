/**
 * StreamPanel.jsx — Stream management panel for the Dashboard view.
 *
 * Handles all three stream source types:
 *   - Playlist streams: ffmpeg concat demuxer → MPEG-TS UDP multicast
 *   - Browser source streams: Podman container with Xvfb + Firefox + ffmpeg x11grab → multicast
 *   - Presentation streams: Podman container with Xvfb + LibreOffice Impress + ffmpeg x11grab → multicast
 *
 * Features:
 *   - Stream tab bar with status dot indicators
 *   - Admin-only stream creation (playlist, browser, or presentation type selector)
 *   - Multicast output configuration (address, port, playback mode)
 *   - Browser source URL/audio configuration
 *   - Presentation source: presentation picker + slide navigation controls
 *   - noVNC iframe for interactive preview (browser and presentation streams)
 *   - Transport controls (Start/Stop/Restart) with live status polling
 *   - Playlist management: reorder items (up/down), remove items
 *
 * RBAC:
 *   - Admin: create/delete streams, edit all configuration
 *   - Assigned users: start/stop/restart streams, manage playlist items
 *
 * The panel polls stream status every 2 seconds to keep the transport controls
 * and PID display up to date without requiring the user to manually refresh.
 */
import React, { useState, useEffect, useCallback } from 'react';
import {
  createStream, updateStream, deleteStream, updateBrowserConfig,
  removePlaylistItem, reorderPlaylist,
  startStream, stopStream, restartStream, getStreamStatus,
  listPresentations, slideControl,
} from '../api';

export default function StreamPanel({ streams, selectedStreamId, onSelectStream, assets, isLoading, onRefresh, isAdmin }) {
  /** True when the playlist has been modified while the stream is running */
  const [playlistDirty, setPlaylistDirty] = useState(false);
  /** Whether the multicast output config is in edit mode */
  const [editing, setEditing] = useState(false);
  /** Form state for multicast output config (name, address, port, playback_mode) */
  const [form, setForm] = useState({ name: '', multicast_address: '', multicast_port: '', playback_mode: 'loop' });
  /** True while a new stream creation request is in-flight */
  const [creating, setCreating] = useState(false);
  /** Source type selection for the "New" button dropdown */
  const [newSourceType, setNewSourceType] = useState('playlist');
  /** Live status object from the /status endpoint (includes PID, runtime info) */
  const [liveStatus, setLiveStatus] = useState(null);
  /** True while a transport control action (start/stop/restart) is in-flight */
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');

  // --- Browser source config state ---
  const [browserUrl, setBrowserUrl] = useState('');
  const [browserAudio, setBrowserAudio] = useState(false);
  /** Whether the browser source config section is in edit mode */
  const [editingBrowser, setEditingBrowser] = useState(false);

  // --- Presentation / slide control state ---
  /** Available presentations loaded from the API */
  const [presentations, setPresentations] = useState([]);
  /** Selected presentation ID in the browser source config form */
  const [selectedPresentationId, setSelectedPresentationId] = useState(null);
  /** Source mode: 'url' for manual URL, 'presentation' for linked presentation */
  const [browserSourceMode, setBrowserSourceMode] = useState('url');

  /** The currently selected stream object (derived from the streams array) */
  const currentStream = streams.find(s => s.id === selectedStreamId);
  /** Convenience flags for source type checks */
  const isBrowser = currentStream?.source_type === 'browser';
  const isPresentation = currentStream?.source_type === 'presentation';
  /** True for source types that use a container (browser or presentation) */
  const isContainerBased = isBrowser || isPresentation;

  /**
   * Polls the stream status endpoint every 2 seconds.
   * This keeps the transport control state (running/stopped), PID display,
   * and container status up to date in real time.
   */
  const pollStatus = useCallback(async () => {
    if (!selectedStreamId) return;
    try { setLiveStatus(await getStreamStatus(selectedStreamId)); } catch {}
  }, [selectedStreamId]);

  // Set up the 2-second polling interval for stream status
  useEffect(() => {
    pollStatus();
    const interval = setInterval(pollStatus, 2000);
    return () => clearInterval(interval);
  }, [pollStatus]);

  /**
   * Syncs the form state whenever the selected stream changes.
   * This ensures the config display/edit fields always reflect the current stream's values.
   */
  useEffect(() => {
    if (currentStream) {
      setForm({
        name: currentStream.name,
        multicast_address: currentStream.multicast_address,
        multicast_port: String(currentStream.multicast_port),
        playback_mode: currentStream.playback_mode,
      });
      // Sync browser/presentation source config for container-based streams
      if (currentStream.browser_source) {
        setBrowserUrl(currentStream.browser_source.url || '');
        setBrowserAudio(currentStream.browser_source.capture_audio || false);
        const presId = currentStream.browser_source.presentation_id;
        setSelectedPresentationId(presId || null);
        // For presentation streams, always default to presentation mode
        setBrowserSourceMode(currentStream.source_type === 'presentation' ? 'presentation' : (presId ? 'presentation' : 'url'));
      }
    }
    // Reset dirty flag when switching streams
    setPlaylistDirty(false);
  }, [selectedStreamId]);

  /** Load available presentations when entering config edit mode for container-based streams */
  useEffect(() => {
    if (editingBrowser || isPresentation) {
      listPresentations().then(data => setPresentations(data.presentations || []))
        .catch(() => setPresentations([]));
    }
  }, [editingBrowser, isPresentation]);

  // ─── CRUD Operations ────────────────────────────────────────────────────────

  /**
   * Creates a new stream with sensible defaults.
   * Admin-only. After creation, auto-selects the new stream in the tab bar.
   */
  const handleCreate = async () => {
    setCreating(true); setErrorMsg('');
    try {
      const defaultNames = { playlist: 'New Stream', browser: 'Browser Source', presentation: 'Presentation' };
      const newStream = await createStream({
        name: defaultNames[newSourceType] || 'New Stream',
        multicast_address: '239.1.1.1', multicast_port: 5000,
        playback_mode: 'loop', source_type: newSourceType,
      });
      await onRefresh();
      onSelectStream(newStream.id);
      setNewSourceType('playlist');
    } catch (e) { setErrorMsg(e.message); }
    finally { setCreating(false); }
  };

  /** Saves the multicast output configuration (name, address, port, playback mode). */
  const handleSave = async () => {
    if (!selectedStreamId) return; setErrorMsg('');
    try {
      await updateStream(selectedStreamId, { ...form, multicast_port: parseInt(form.multicast_port, 10) });
      setEditing(false); onRefresh();
    } catch (e) { setErrorMsg(e.message); }
  };

  /** Saves the browser/presentation source configuration. */
  const handleSaveBrowser = async () => {
    if (!selectedStreamId) return; setErrorMsg('');
    try {
      // Presentation streams always pass the selected presentation ID
      const presId = isPresentation ? selectedPresentationId : null;
      const url = isPresentation ? 'about:blank' : browserUrl;
      await updateBrowserConfig(selectedStreamId, url, browserAudio, presId);
      setEditingBrowser(false); onRefresh();
    } catch (e) { setErrorMsg(e.message); }
  };

  /** Deletes the selected stream after a browser confirm dialog. */
  const handleDelete = async () => {
    if (!window.confirm('Delete this stream?')) return;
    try { await deleteStream(selectedStreamId); onSelectStream(null); onRefresh(); }
    catch (e) { setErrorMsg(e.message); }
  };

  // ─── Playlist Operations ────────────────────────────────────────────────────

  const handleRemoveItem = async (itemId) => {
    try {
      await removePlaylistItem(selectedStreamId, itemId);
      if (isRunning) setPlaylistDirty(true);
      onRefresh();
    } catch (e) { setErrorMsg(e.message); }
  };

  /**
   * Moves a playlist item up or down by swapping it with its neighbor.
   * Sends the entire reordered asset ID list to the backend (not a delta).
   */
  const handleMoveItem = async (idx, dir) => {
    if (!currentStream) return;
    const items = [...currentStream.items];
    const target = idx + dir;
    if (target < 0 || target >= items.length) return;
    // Swap the two items in the local copy
    [items[idx], items[target]] = [items[target], items[idx]];
    try {
      await reorderPlaylist(selectedStreamId, items.map(i => i.asset_id));
      if (isRunning) setPlaylistDirty(true);
      onRefresh();
    } catch (e) { setErrorMsg(e.message); }
  };

  // ─── Transport Controls ─────────────────────────────────────────────────────

  /**
   * Generic wrapper for transport actions (start/stop/restart).
   * Sets busy state to disable buttons during the request and shows errors on failure.
   */
  const doAction = async (fn) => {
    setBusy(true); setErrorMsg('');
    try {
      await fn();
      setPlaylistDirty(false);
      onRefresh();
    } catch (e) { setErrorMsg(e.message); }
    finally { setBusy(false); }
  };

  /** True if the stream is currently running or in the process of starting */
  const isRunning = currentStream?.status === 'running' || currentStream?.status === 'starting';
  /** True if the playlist has at least one fully transcoded asset (required to start) */
  const hasReadyItems = currentStream?.items?.some(i => i.asset.status === 'ready');
  /**
   * Determines whether the Start button should be enabled:
   *   - Browser streams: need a valid URL configured (not the default about:blank)
   *   - Presentation streams: need a presentation linked
   *   - Playlist streams: need at least one ready (fully transcoded) asset
   */
  const canStart = isBrowser
    ? (currentStream?.browser_source?.url && currentStream.browser_source.url !== 'about:blank')
    : isPresentation
      ? !!currentStream?.browser_source?.presentation_id
      : hasReadyItems;

  /**
   * Constructs the noVNC URL for the browser source preview iframe.
   * Proxied through nginx at /novnc/<port>/ so it stays on the same origin and
   * protocol (HTTPS), avoiding mixed-content blocks in the browser.
   * Query params configure auto-connect, viewport scaling, auto-reconnect, and cursor dot.
   */
  const novncPort = currentStream?.browser_source?.novnc_port;
  const novncUrl = (novncPort && isRunning)
    ? `${window.location.origin}/novnc/${novncPort}/vnc_embed.html?autoconnect=true&reconnect=true&show_dot=true`
    : null;

  if (isLoading) {
    return (<div className="stream-panel"><div className="panel-header"><h2>Stream Output</h2></div>
      <div className="loading-state">Loading...</div></div>);
  }

  return (
    <div className="stream-panel">
      <div className="panel-header">
        <h2>Stream Output</h2>
        {/* Admin-only: source type selector + create button */}
        {isAdmin && (
          <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
            <select className="source-type-select" value={newSourceType}
              onChange={e => setNewSourceType(e.target.value)}>
              <option value="playlist">Playlist</option>
              <option value="browser">Browser</option>
              <option value="presentation">Presentation</option>
            </select>
            <button className="btn btn-sm btn-accent" onClick={handleCreate} disabled={creating}>
              + New
            </button>
          </div>
        )}
      </div>

      {/* Stream tab bar — each tab shows a status dot (green=running, etc.) and stream name */}
      {streams.length > 0 && (
        <div className="stream-tabs">
          {streams.map(s => (
            <button key={s.id}
              className={`stream-tab ${s.id === selectedStreamId ? 'active' : ''}`}
              onClick={() => onSelectStream(s.id)}>
              <span className={`status-dot status-${s.status}`} />
              {s.source_type === 'browser' ? '🌐 ' : s.source_type === 'presentation' ? '📊 ' : ''}{s.name}
            </button>
          ))}
        </div>
      )}

      {errorMsg && <div className="panel-error">{errorMsg}</div>}

      {!currentStream ? (
        <div className="empty-state">
          {/* Different empty state messages for admin vs regular user */}
          {streams.length === 0
            ? (isAdmin ? 'No streams configured. Create one to get started.'
                       : 'No channels assigned to you. Ask an administrator.')
            : 'Select a stream above.'}
        </div>
      ) : (<>
        {/* ── Multicast output configuration ─────────────────────────────────── */}
        <div className="config-section">
          <div className="config-header">
            <h3>Multicast Output</h3>
            {/* Edit/Save/Cancel toggle — admin-only */}
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
              {/* Playback mode is only relevant for playlist streams (container streams are continuous) */}
              {!isContainerBased && (
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
                <span className={`badge badge-sm ${isContainerBased ? 'badge-info' : 'badge-success'}`}>
                  {isBrowser ? 'Browser Source' : isPresentation ? 'Presentation' : 'Playlist'}
                </span></div>
              <div className="config-value"><span className="config-label">Destination</span>
                <span className="mono config-addr">udp://{currentStream.multicast_address}:{currentStream.multicast_port}</span></div>
              {!isContainerBased && (
                <div className="config-value"><span className="config-label">Mode</span>
                  <span>{currentStream.playback_mode === 'loop' ? 'Loop' : 'One-shot'}</span></div>
              )}
            </div>
          )}
        </div>

        {/* ── Browser source configuration (only for browser-type streams) ───── */}
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

        {/* ── Presentation source configuration (only for presentation-type streams) ── */}
        {isPresentation && (
          <div className="config-section">
            <div className="config-header">
              <h3>Presentation Source</h3>
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
                  <div className="form-group"><label>Presentation</label>
                    <select value={selectedPresentationId || ''}
                      onChange={e => setSelectedPresentationId(e.target.value ? Number(e.target.value) : null)}>
                      <option value="">-- Select a presentation --</option>
                      {presentations.filter(p => p.status === 'ready').map(p => (
                        <option key={p.id} value={p.id}>{p.name}</option>
                      ))}
                    </select>
                    {presentations.filter(p => p.status === 'ready').length === 0 && (
                      <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
                        Upload a presentation in the Media Library first.
                      </span>
                    )}
                  </div>
                </div>
                <div className="form-row">
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13,
                    color: 'var(--text-secondary)', padding: '4px 0' }}>
                    <input type="checkbox" checked={browserAudio}
                      onChange={e => setBrowserAudio(e.target.checked)} />
                    Capture presentation audio (embedded media)
                  </label>
                </div>
              </div>
            ) : (
              <div className="config-display">
                {currentStream.remote_control ? (
                  <div className="config-value"><span className="config-label">Presentation</span>
                    <span>{currentStream.remote_control.presentation_name}</span></div>
                ) : (
                  <div className="config-value"><span className="config-label">Presentation</span>
                    <span style={{ color: 'var(--text-muted)' }}>Not selected</span></div>
                )}
                <div className="config-value"><span className="config-label">Audio</span>
                  <span>{currentStream.browser_source?.capture_audio ? 'Capturing' : 'Disabled'}</span></div>
              </div>
            )}
          </div>
        )}

        {/*
          noVNC interactive preview — embedded iframe connecting to websockify in the container.
          Shown for both browser and presentation streams when the container is running.
          Connects directly to the port (bypassing nginx) due to the nginx proxy limitation.
        */}
        {isContainerBased && isRunning && novncPort && (
          <div className="browser-preview">
            <div className="config-header">
              <h3>{isPresentation ? 'Live Presentation View' : 'Live Browser View (interactive)'}</h3>
            </div>
            <iframe
              src={novncUrl}
              title={isPresentation ? 'Presentation Preview' : 'Browser Source'}
              className="novnc-frame"
              allow="clipboard-read; clipboard-write"
            />
          </div>
        )}

        {/* ── Slide navigation controls (presentation streams only, when running) */}
        {isPresentation && isRunning && (
          <div className="slide-controls">
            <div className="config-header">
              <h3>Slide Control{currentStream.remote_control
                ? ` — ${currentStream.remote_control.presentation_name}` : ''}</h3>
            </div>
            <div className="slide-nav">
              <button className="btn btn-sm"
                title="First slide (Home)"
                onClick={async () => {
                  try { await slideControl(selectedStreamId, 'first'); }
                  catch (e) { setErrorMsg(e.message); }
                }}>
                |◀ First
              </button>
              <button className="btn btn-sm"
                title="Previous slide/animation (Left arrow)"
                onClick={async () => {
                  try { await slideControl(selectedStreamId, 'prev'); }
                  catch (e) { setErrorMsg(e.message); }
                }}>
                ◀ Prev
              </button>
              <button className="btn btn-sm"
                title="Next slide/animation (Right arrow)"
                onClick={async () => {
                  try { await slideControl(selectedStreamId, 'next'); }
                  catch (e) { setErrorMsg(e.message); }
                }}>
                Next ▶
              </button>
              <button className="btn btn-sm"
                title="Last slide (End)"
                onClick={async () => {
                  try { await slideControl(selectedStreamId, 'last'); }
                  catch (e) { setErrorMsg(e.message); }
                }}>
                Last ▶|
              </button>
            </div>
          </div>
        )}

        {/* ── Transport controls (Start/Stop/Restart + status indicator) ──── */}
        <div className="transport-section">
          <div className="transport-status">
            <span className={`status-indicator status-${currentStream.status}`}>
              {currentStream.status.toUpperCase()}
            </span>
            {/* Show the ffmpeg PID (for playlist streams) or container PID (for browser streams) */}
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
                title={!canStart ? (isBrowser ? 'Configure a URL first' : isPresentation ? 'Select a presentation first' : 'Add ready assets first') : ''}>
                ▶ Start
              </button>
            )}
            {/* Delete button — admin-only, always visible regardless of stream state */}
            {isAdmin && (
              <button className="btn btn-ghost btn-delete" onClick={handleDelete}>🗑</button>
            )}
          </div>
        </div>

        {/* ── Playlist section (only for playlist-type streams) ──────────── */}
        {!isContainerBased && (
          <div className="playlist-section">
            <h3>Playlist ({currentStream.items.length} items)</h3>
            {playlistDirty && isRunning && (
              <div className="playlist-dirty-notice">
                Playlist modified — restart the stream to apply changes.
              </div>
            )}
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
                    {/* Reorder (up/down) and remove buttons for each playlist item */}
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
