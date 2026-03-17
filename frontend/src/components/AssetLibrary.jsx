/**
 * AssetLibrary.jsx — Media library panel for the Dashboard view.
 *
 * Features:
 *   - Drag-and-drop file upload with real-time progress bar (via XHR in api.js)
 *   - Click-to-browse file picker as an alternative to drag-drop
 *   - Storage usage indicator with color thresholds (green < 70%, yellow < 90%, red >= 90%)
 *   - Asset grid displaying thumbnails, metadata (duration, resolution, file size), and status
 *   - Inline rename: click an asset name to edit it in place (Enter to save, Escape to cancel)
 *   - Transcode progress: assets in 'processing' status show a live progress bar
 *     (the parent Layout component polls every 2s while any asset is processing)
 *   - "+ Stream" button to add ready assets to the currently selected stream's playlist
 *   - Two-step delete confirmation to prevent accidental deletions
 *
 * Props:
 *   - assets: array of asset objects from the backend
 *   - isLoading: true while the initial asset fetch is in-flight
 *   - onRefresh: callback to re-fetch assets and storage info
 *   - selectedStreamId: ID of the currently selected stream (for "+ Stream" button)
 *   - onRefreshStreams: callback to re-fetch streams (after adding/removing playlist items)
 *   - storage: storage usage info object (usable_gb, usage_percent, etc.)
 *   - isAdmin: boolean for RBAC (currently unused here but passed through for future use)
 */
import React, { useState, useRef } from 'react';
import { uploadAsset, deleteAsset, renameAsset, addPlaylistItem } from '../api';

/**
 * Formats a byte count into a human-readable string (e.g., 1536000 → "1.5 MB").
 * Uses base-1024 divisions (KiB-style) with SI labels for simplicity.
 */
function formatSize(bytes) {
  if (!bytes) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let unitIndex = 0, size = bytes;
  while (size >= 1024 && unitIndex < units.length - 1) { size /= 1024; unitIndex++; }
  return `${size.toFixed(1)} ${units[unitIndex]}`;
}

/**
 * Formats seconds into a human-readable duration string (e.g., 3661 → "1:01:01").
 * Uses HH:MM:SS format when hours > 0, otherwise MM:SS.
 */
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

/** Maps asset status strings to CSS badge classes and display labels for the status overlay. */
const STATUS_BADGE = {
  uploading:  { cls: 'badge-info',    label: 'Uploading' },
  processing: { cls: 'badge-warning', label: 'Transcoding' },
  ready:      { cls: 'badge-success', label: 'Ready' },
  error:      { cls: 'badge-error',   label: 'Error' },
};

export default function AssetLibrary({ assets, isLoading, onRefresh, selectedStreamId, onRefreshStreams, storage, isAdmin }) {
  /** Upload progress percentage (0-100), or null when no upload is in progress */
  const [uploadProgress, setUploadProgress] = useState(null);
  const [uploadError, setUploadError] = useState('');
  /** True when a file is being dragged over the drop zone (for visual feedback) */
  const [isDragOver, setIsDragOver] = useState(false);
  /** ID of the asset showing the two-step delete confirmation, or null */
  const [deleteConfirmId, setDeleteConfirmId] = useState(null);
  /** ID of the asset currently being renamed inline, or null */
  const [renamingId, setRenamingId] = useState(null);
  /** Current text in the inline rename input field */
  const [renameValue, setRenameValue] = useState('');
  /** Ref to the hidden file input so the drop zone click can trigger it programmatically */
  const fileInputRef = useRef(null);

  /**
   * Processes an array of files for upload. Files are uploaded sequentially (not in parallel)
   * to avoid overwhelming the server and to show clear per-file progress.
   */
  const handleFiles = async (files) => {
    setUploadError('');
    for (const file of files) {
      try {
        setUploadProgress(0);
        await uploadAsset(file, (pct) => setUploadProgress(pct));
        setUploadProgress(null);
        // Refresh the asset list to show the newly uploaded asset (in 'processing' state)
        onRefresh();
      } catch (err) { setUploadError(err.message); setUploadProgress(null); }
    }
  };

  /** Handles the native HTML5 drop event on the upload zone. */
  const onDrop = (e) => { e.preventDefault(); setIsDragOver(false); handleFiles(Array.from(e.dataTransfer.files)); };
  /** Handles file selection from the hidden <input type="file"> triggered by clicking the drop zone. */
  const onFileSelect = (e) => { handleFiles(Array.from(e.target.files)); e.target.value = ''; };

  /** Deletes an asset and refreshes both the asset list and streams (playlist items may reference it). */
  const doDelete = async (assetId) => {
    try { await deleteAsset(assetId); setDeleteConfirmId(null); onRefresh(); onRefreshStreams(); }
    catch (err) { console.error('Delete failed:', err); }
  };

  /** Adds an asset to the currently selected stream's playlist. */
  const doAddToStream = async (assetId) => {
    if (!selectedStreamId) return;
    try { await addPlaylistItem(selectedStreamId, assetId); onRefreshStreams(); }
    catch (err) { console.error('Add to stream failed:', err); }
  };

  /** Enters inline rename mode for an asset, pre-populating the input with the current name. */
  const startRename = (asset) => {
    setRenamingId(asset.id);
    setRenameValue(asset.display_name);
  };

  /** Commits the inline rename if the value is non-empty. */
  const doRename = async (assetId) => {
    if (!renameValue.trim()) { setRenamingId(null); return; }
    try {
      await renameAsset(assetId, renameValue.trim());
      setRenamingId(null);
      onRefresh();
      // Refresh streams too because playlist items display asset names
      onRefreshStreams();
    } catch (err) { console.error('Rename failed:', err); }
  };

  /** Keyboard handler for the rename input: Enter to save, Escape to cancel. */
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

      {/* Storage usage bar — color shifts from accent (green) → warning (yellow) → error (red) */}
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
                // Color thresholds: red when nearly full, yellow when getting full, green otherwise
                background: storage.usage_percent > 90 ? 'var(--status-error)' :
                            storage.usage_percent > 70 ? 'var(--status-warning)' : 'var(--accent)'
              }} />
          </div>
        </div>
      )}

      {/*
        Upload drop zone — serves double duty as both a drag-drop target and a click-to-browse
        button. The hidden <input type="file"> is triggered programmatically on click.
        Accepts video/*, image/*, and audio/* MIME types.
      */}
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

      {/* Asset grid — each card shows thumbnail, metadata, status, and action buttons */}
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
                  {/* Thumbnails are served without auth (img tags can't send JWT headers) */}
                  {asset.thumbnail_url
                    ? <img src={asset.thumbnail_url} alt="" loading="lazy" />
                    : <div className="asset-thumb-placeholder">{asset.asset_type === 'image' ? '🖼' : asset.asset_type === 'audio' ? '🎵' : '🎬'}</div>}
                  <span className={`badge ${badge.cls}`}>{badge.label}</span>
                </div>
                <div className="asset-info">
                  {/* Inline rename: clicking the name enters edit mode */}
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
                    {/* Show duration, resolution, and file size only for fully transcoded assets */}
                    {isReady && (<>
                      <span className="mono">{formatDuration(asset.duration_seconds)}</span>
                      <span className="meta-dot">·</span>
                      <span className="mono">{asset.width}×{asset.height}</span>
                      <span className="meta-dot">·</span>
                      <span>{formatSize(asset.file_size_bytes)}</span>
                    </>)}
                    {/* Live transcode progress bar — updated by the 2s polling in Layout */}
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
                  {/* Only show "+ Stream" for ready assets when a stream is selected */}
                  {isReady && selectedStreamId && (
                    <button className="btn btn-sm btn-accent" onClick={() => doAddToStream(asset.id)}>+ Stream</button>
                  )}
                  {/* Two-step delete: first click shows Confirm/Cancel, second click deletes */}
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
