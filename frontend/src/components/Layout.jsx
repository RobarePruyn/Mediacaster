/**
 * Main layout with RBAC-aware navigation.
 * Admin sees: Dashboard, Monitoring, Settings (with User Management)
 * User sees: Dashboard, Settings (Account only)
 */
import React, { useState, useEffect, useCallback } from 'react';
import AssetLibrary from './AssetLibrary';
import StreamPanel from './StreamPanel';
import Settings from './Settings';
import Monitoring from './Monitoring';
import { listAssets, listStreams, getStorageInfo } from '../api';

export default function Layout({ currentUser, onLogout }) {
  const isAdmin = currentUser?.is_admin;
  const [activeView, setActiveView] = useState('dashboard');
  const [assets, setAssets] = useState([]);
  const [streams, setStreams] = useState([]);
  const [selectedStreamId, setSelectedStreamId] = useState(null);
  const [loadingAssets, setLoadingAssets] = useState(true);
  const [loadingStreams, setLoadingStreams] = useState(true);
  const [storage, setStorage] = useState(null);

  const refreshAssets = useCallback(async () => {
    try { const d = await listAssets(); setAssets(d.assets); }
    catch (e) { console.error('Failed to load assets:', e); }
    finally { setLoadingAssets(false); }
  }, []);

  const refreshStreams = useCallback(async () => {
    try {
      const d = await listStreams();
      setStreams(d.streams);
      if (d.streams.length > 0 && selectedStreamId === null) setSelectedStreamId(d.streams[0].id);
    } catch (e) { console.error('Failed to load streams:', e); }
    finally { setLoadingStreams(false); }
  }, [selectedStreamId]);

  const refreshStorage = useCallback(async () => {
    try { setStorage(await getStorageInfo()); } catch {}
  }, []);

  useEffect(() => { refreshAssets(); refreshStreams(); refreshStorage(); }, [refreshAssets, refreshStreams, refreshStorage]);

  // Poll for transcode progress
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
          <section className="panel panel-library">
            <AssetLibrary assets={assets} isLoading={loadingAssets} onRefresh={() => { refreshAssets(); refreshStorage(); }}
              selectedStreamId={selectedStreamId} onRefreshStreams={refreshStreams}
              storage={storage} isAdmin={isAdmin} />
          </section>
          <section className="panel panel-streams">
            <StreamPanel streams={streams} selectedStreamId={selectedStreamId}
              onSelectStream={setSelectedStreamId} assets={assets}
              isLoading={loadingStreams} onRefresh={refreshStreams}
              isAdmin={isAdmin} />
          </section>
        </main>
      )}

      {activeView === 'monitoring' && isAdmin && (
        <main className="single-panel"><Monitoring /></main>
      )}

      {activeView === 'settings' && (
        <main className="single-panel"><Settings currentUser={currentUser} /></main>
      )}
    </div>
  );
}
