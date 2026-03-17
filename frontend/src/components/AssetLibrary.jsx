/**
 * Media Library panel — drag-drop upload, browse, rename, delete, add to stream.
 */
import React, { useState, useRef } from 'react';
import { uploadAsset, deleteAsset, renameAsset, addPlaylistItem } from '../api';

function formatSize(bytes) {
  if (!bytes) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let unitIndex = 0, size = bytes;
  while (size >= 1024 && unitIndex < units.length - 1) { size /= 1024; unitIndex++; }
  return `${size.toFixed(1)} ${units[unitIndex]}`;
}

function formatDuration(seconds) {
  if (!seconds) return '—';
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

const STATUS_BADGE = {
  uploading:  { cls: 'badge-info',    label: 'Uploading' },
  processing: { cls: 'badge-warning', label: 'Transcoding' },
  ready:      { cls: 'badge-success', label: 'Ready' },
  error:      { cls: 'badge-error',   label: 'Error' },
};

export default function AssetLibrary({ assets, isLoading, onRefresh, selectedStreamId, onRefreshStreams, storage, isAdmin }) {
  const [uploadProgress, setUploadProgress] = useState(null);
  const [uploadError, setUploadError] = useState('');
  const [isDragOver, setIsDragOver] = useState(false);
  const [deleteConfirmId, setDeleteConfirmId] = useState(null);
  const [renamingId, setRenamingId] = useState(null);
  const [renameValue, setRenameValue] = useState('');
  const fileInputRef = useRef(null);

  const handleFiles = async (files) => {
    setUploadError('');
    for (const file of files) {
      try {
        setUploadProgress(0);
        await uploadAsset(file, (pct) => setUploadProgress(pct));
        setUploadProgress(null);
        onRefresh();
      } catch (err) { setUploadError(err.message); setUploadProgress(null); }
    }
  };

  const onDrop = (e) => { e.preventDefault(); setIsDragOver(false); handleFiles(Array.from(e.dataTransfer.files)); };
  const onFileSelect = (e) => { handleFiles(Array.from(e.target.files)); e.target.value = ''; };

  const doDelete = async (assetId) => {
    try { await deleteAsset(assetId); setDeleteConfirmId(null); onRefresh(); onRefreshStreams(); }
    catch (err) { console.error('Delete failed:', err); }
  };

  const doAddToStream = async (assetId) => {
    if (!selectedStreamId) return;
    try { await addPlaylistItem(selectedStreamId, assetId); onRefreshStreams(); }
    catch (err) { console.error('Add to stream failed:', err); }
  };

  const startRename = (asset) => {
    setRenamingId(asset.id);
    setRenameValue(asset.display_name);
  };

  const doRename = async (assetId) => {
    if (!renameValue.trim()) { setRenamingId(null); return; }
    try {
      await renameAsset(assetId, renameValue.trim());
      setRenamingId(null);
      onRefresh();
      onRefreshStreams(); // Playlist names may have changed
    } catch (err) { console.error('Rename failed:', err); }
  };

  const handleRenameKey = (e, assetId) => {
    if (e.key === 'Enter') doRename(assetId);
    if (e.key === 'Escape') setRenamingId(null);
  };

  return (
    <div className="asset-library">
      <div className="panel-header">
        <h2>Media Library</h2>
        <span className="panel-count">{assets.length} assets</span>
      </div>

      {/* Storage indicator */}
      {storage && (
        <div className="storage-bar-wrapper">
          <div className="storage-bar-header">
            <span className="storage-label">Storage</span>
            <span className="mono storage-numbers">
              {storage.usable_remaining_gb} GB free of {storage.usable_gb} GB
            </span>
          </div>
          <div className="storage-bar-track">
            <div className="storage-bar-fill"
              style={{
                width: `${Math.min(storage.usage_percent, 100)}%`,
                background: storage.usage_percent > 90 ? 'var(--status-error)' :
                            storage.usage_percent > 70 ? 'var(--status-warning)' : 'var(--accent)'
              }} />
          </div>
        </div>
      )}

      {/* Upload drop zone */}
      <div className={`upload-zone ${isDragOver ? 'upload-zone-active' : ''}`}
        onDrop={onDrop}
        onDragOver={(e) => { e.preventDefault(); setIsDragOver(true); }}
        onDragLeave={() => setIsDragOver(false)}
        onClick={() => fileInputRef.current?.click()}>
        <input ref={fileInputRef} type="file" accept="video/*,image/*,audio/*" multiple
          onChange={onFileSelect} style={{ display: 'none' }} />
        {uploadProgress !== null ? (
          <div className="upload-progress">
            <div className="upload-progress-bar">
              <div className="upload-progress-fill" style={{ width: `${uploadProgress}%` }} />
            </div>
            <span className="upload-progress-text">{uploadProgress}% uploaded</span>
          </div>
        ) : (<>
          <span className="upload-icon">⬆</span>
          <span className="upload-text">Drop files here or click to browse</span>
          <span className="upload-hint">Video: mp4, mov, avi, mkv, ts — Image: jpg, png, webp — Audio: mp3, wav, flac, aac, ogg</span>
        </>)}
      </div>

      {uploadError && <div className="upload-error">{uploadError}</div>}

      {/* Asset list */}
      {isLoading ? <div className="loading-state">Loading library...</div>
       : assets.length === 0 ? <div className="empty-state">No assets yet. Upload a video or image to get started.</div>
       : (
        <div className="asset-grid">
          {assets.map((asset) => {
            const badge = STATUS_BADGE[asset.status] || STATUS_BADGE.error;
            const isReady = asset.status === 'ready';
            const isRenaming = renamingId === asset.id;
            return (
              <div key={asset.id} className="asset-card">
                <div className="asset-thumb">
                  {asset.thumbnail_url
                    ? <img src={asset.thumbnail_url} alt="" loading="lazy" />
                    : <div className="asset-thumb-placeholder">{asset.asset_type === 'image' ? '🖼' : asset.asset_type === 'audio' ? '🎵' : '🎬'}</div>}
                  <span className={`badge ${badge.cls}`}>{badge.label}</span>
                </div>
                <div className="asset-info">
                  {isRenaming ? (
                    <input
                      className="rename-input"
                      type="text"
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onKeyDown={(e) => handleRenameKey(e, asset.id)}
                      onBlur={() => doRename(asset.id)}
                      autoFocus
                    />
                  ) : (
                    <span
                      className="asset-name"
                      title={`${asset.display_name} (click to rename)`}
                      onClick={() => startRename(asset)}
                      style={{ cursor: 'pointer' }}
                    >
                      {asset.display_name}
                    </span>
                  )}
                  <div className="asset-meta">
                    {isReady && (<>
                      <span className="mono">{formatDuration(asset.duration_seconds)}</span>
                      <span className="meta-dot">·</span>
                      <span className="mono">{asset.width}×{asset.height}</span>
                      <span className="meta-dot">·</span>
                      <span>{formatSize(asset.file_size_bytes)}</span>
                    </>)}
                    {asset.status === 'processing' && (
                      <div className="transcode-progress">
                        <div className="transcode-progress-bar">
                          <div className="transcode-progress-fill"
                            style={{ width: `${asset.transcode_progress || 0}%` }} />
                        </div>
                        <span className="processing-text mono">
                          {Math.round(asset.transcode_progress || 0)}%
                        </span>
                      </div>
                    )}
                    {asset.status === 'error' && <span className="error-text" title={asset.error_message}>Transcode failed</span>}
                  </div>
                </div>
                <div className="asset-actions">
                  {isReady && selectedStreamId && (
                    <button className="btn btn-sm btn-accent" onClick={() => doAddToStream(asset.id)}>+ Stream</button>
                  )}
                  {deleteConfirmId === asset.id ? (
                    <div className="delete-confirm">
                      <button className="btn btn-sm btn-danger" onClick={() => doDelete(asset.id)}>Confirm</button>
                      <button className="btn btn-sm btn-ghost" onClick={() => setDeleteConfirmId(null)}>Cancel</button>
                    </div>
                  ) : (
                    <button className="btn btn-sm btn-ghost btn-delete"
                      onClick={() => setDeleteConfirmId(asset.id)} title="Delete">✕</button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
