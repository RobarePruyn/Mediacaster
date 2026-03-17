/**
 * Layout.jsx — Main application shell with RBAC-aware navigation.
 *
 * Renders the top navigation bar and manages which view is displayed:
 *   - Dashboard (all users): side-by-side AssetLibrary + StreamPanel
 *   - Monitoring (admin only): system resource metrics
 *   - Settings (all users, but admin sees additional tabs for users and server config)
 *
 * Also owns the top-level data fetching for assets, streams, and storage info so that
 * the Dashboard child components share a single source of truth and can trigger refreshes
 * via callbacks rather than each maintaining their own fetch cycle.
 */
import React, { useState, useEffect, useCallback } from 'react';
import AssetLibrary from './AssetLibrary';
import StreamPanel from './StreamPanel';
import Settings from './Settings';
import Monitoring from './Monitoring';
import { listAssets, listStreams, getStorageInfo } from '../api';

export default function Layout({ currentUser, onLogout }) {
  const isAdmin = currentUser?.is_admin;
  /** Which top-level view is active: 'dashboard' | 'monitoring' | 'settings' */
  const [activeView, setActiveView] = useState('dashboard');
  const [assets, setAssets] = useState([]);
  const [streams, setStreams] = useState([]);
  /** ID of the stream currently selected in the StreamPanel tab bar */
  const [selectedStreamId, setSelectedStreamId] = useState(null);
  const [loadingAssets, setLoadingAssets] = useState(true);
  const [loadingStreams, setLoadingStreams] = useState(true);
  /** Storage usage info (usable_gb, usage_percent, etc.) for the storage bar in AssetLibrary */
  const [storage, setStorage] = useState(null);

  /** Fetches the asset list. Called on mount and after uploads/deletes/renames. */
  const refreshAssets = useCallback(async () => {
    try { const d = await listAssets(); setAssets(d.assets); }
    catch (e) { console.error('Failed to load assets:', e); }
    finally { setLoadingAssets(false); }
  }, []);

  /**
   * Fetches the stream list. Auto-selects the first stream if none is currently selected,
   * so users always see a stream panel on first load rather than an empty state.
   */
  const refreshStreams = useCallback(async () => {
    try {
      const d = await listStreams();
      setStreams(d.streams);
      // Auto-select the first stream on initial load (selectedStreamId starts as null)
      if (d.streams.length > 0 && selectedStreamId === null) setSelectedStreamId(d.streams[0].id);
    } catch (e) { console.error('Failed to load streams:', e); }
    finally { setLoadingStreams(false); }
  }, [selectedStreamId]);

  /** Fetches disk storage info for the storage usage bar in AssetLibrary. */
  const refreshStorage = useCallback(async () => {
    try { setStorage(await getStorageInfo()); } catch {}
  }, []);

  // Initial data load on mount
  useEffect(() => { refreshAssets(); refreshStreams(); refreshStorage(); }, [refreshAssets, refreshStreams, refreshStorage]);

  /**
   * Polling interval for transcode progress.
   * Only active when at least one asset is in 'uploading' or 'processing' state.
   * Polls every 2 seconds to update progress bars in the asset library, then stops
   * automatically once all assets reach 'ready' or 'error' status.
   */
  useEffect(() => {
    const processing = assets.some(a => a.status === 'uploading' || a.status === 'processing');
    if (!processing) return;
    const interval = setInterval(refreshAssets, 2000);
    return () => clearInterval(interval);
  }, [assets, refreshAssets]);

  return (
    <div className="layout">
      <header className="topbar">
        <div className="topbar-left">
          <span className="topbar-icon">▶</span>
          <h1 className="topbar-title">Multicast Streamer</h1>
          <nav className="topbar-nav">
            <button className={`nav-btn ${activeView === 'dashboard' ? 'active' : ''}`}
              onClick={() => setActiveView('dashboard')}>Dashboard</button>
            {/* Monitoring tab is admin-only — regular users have no access to server metrics */}
            {isAdmin && (
              <button className={`nav-btn ${activeView === 'monitoring' ? 'active' : ''}`}
                onClick={() => setActiveView('monitoring')}>Monitoring</button>
            )}
            <button className={`nav-btn ${activeView === 'settings' ? 'active' : ''}`}
              onClick={() => setActiveView('settings')}>Settings</button>
          </nav>
        </div>
        <div className="topbar-right">
          <span className="topbar-user">{currentUser?.username}</span>
          {isAdmin && <span className="badge badge-sm badge-warning">admin</span>}
          <button className="btn btn-ghost" onClick={onLogout}>Sign Out</button>
        </div>
      </header>

      {activeView === 'dashboard' && (
        <main className="dashboard">
          {/* Left panel: media library with upload, browse, rename, and add-to-stream */}
          <section className="panel panel-library">
            <AssetLibrary assets={assets} isLoading={loadingAssets} onRefresh={() => { refreshAssets(); refreshStorage(); }}
              selectedStreamId={selectedStreamId} onRefreshStreams={refreshStreams}
              storage={storage} isAdmin={isAdmin} />
          </section>
          {/* Right panel: stream configuration, transport controls, playlist / browser config */}
          <section className="panel panel-streams">
            <StreamPanel streams={streams} selectedStreamId={selectedStreamId}
              onSelectStream={setSelectedStreamId} assets={assets}
              isLoading={loadingStreams} onRefresh={refreshStreams}
              isAdmin={isAdmin} />
          </section>
        </main>
      )}

      {/* Guard: only render Monitoring if user is actually admin (defense in depth) */}
      {activeView === 'monitoring' && isAdmin && (
        <main className="single-panel"><Monitoring /></main>
      )}

      {activeView === 'settings' && (
        <main className="single-panel"><Settings currentUser={currentUser} /></main>
      )}
    </div>
  );
}
