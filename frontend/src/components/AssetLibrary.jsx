/**
 * AssetLibrary.jsx — Media library panel with folder tree, search, and sort.
 *
 * Features:
 *   - Folder sidebar with nested tree, create/rename/delete/share controls
 *   - Breadcrumb navigation for the current folder path
 *   - Drag-and-drop file upload with real-time progress bar
 *   - Search bar (filters by display name) and sort controls (name, date, size, type)
 *   - Asset grid with thumbnails, metadata, status badges, inline rename
 *   - Move assets between folders via dropdown
 *   - "+ Stream" button to add ready assets to the currently selected playlist
 *   - Two-step delete confirmation
 *
 * Props:
 *   - assets: array of asset objects from the parent (for polling/processing detection)
 *   - isLoading: true while the initial asset fetch is in-flight
 *   - onRefresh: callback to re-fetch assets and storage info (parent-level)
 *   - selectedStreamId: ID of the currently selected stream (for "+ Stream" button)
 *   - onRefreshStreams: callback to re-fetch streams
 *   - storage: storage usage info object
 *   - isAdmin: boolean for RBAC
 */
import React, { useState, useRef, useEffect, useCallback } from 'react';
import {
  uploadAsset, deleteAsset, renameAsset, addPlaylistItem, listAssets,
  getFolderTree, createFolder, updateFolder, deleteFolder,
  updateFolderSharing, moveAssets, uploadPresentation,
} from '../api';

/* ── Utility functions ───────────────────────────────────────────────────── */

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

/* ── FolderTreeNode — recursive tree item ────────────────────────────────── */

function FolderTreeNode({ folder, selectedFolderId, onSelect, depth = 0, isAdmin,
                          onRename, onDelete, onShare }) {
  const [expanded, setExpanded] = useState(true);
  const hasChildren = folder.children && folder.children.length > 0;
  const isSelected = selectedFolderId === folder.id;

  return (
    <div className="folder-tree-branch">
      <div
        className={`folder-tree-item ${isSelected ? 'folder-tree-item-active' : ''}`}
        style={{ paddingLeft: `${12 + depth * 16}px` }}
        onClick={() => onSelect(folder.id)}
      >
        {/* Expand/collapse toggle for folders with children */}
        <span
          className={`folder-expand ${hasChildren ? 'has-children' : ''}`}
          onClick={(e) => { e.stopPropagation(); if (hasChildren) setExpanded(!expanded); }}
        >
          {hasChildren ? (expanded ? '▾' : '▸') : ' '}
        </span>
        <span className="folder-icon">{folder.is_shared ? '📂' : '📁'}</span>
        <span className="folder-tree-name" title={folder.name}>{folder.name}</span>
        {folder.asset_count > 0 && (
          <span className="folder-tree-count">{folder.asset_count}</span>
        )}
      </div>
      {/* Expanded children */}
      {expanded && hasChildren && (
        <div className="folder-tree-children">
          {folder.children.map(child => (
            <FolderTreeNode
              key={child.id} folder={child} selectedFolderId={selectedFolderId}
              onSelect={onSelect} depth={depth + 1} isAdmin={isAdmin}
              onRename={onRename} onDelete={onDelete} onShare={onShare}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Main component ──────────────────────────────────────────────────────── */

export default function AssetLibrary({ assets, isLoading, onRefresh, selectedStreamId,
                                        onRefreshStreams, storage, isAdmin }) {
  // Upload state
  const [uploadProgress, setUploadProgress] = useState(null);
  const [uploadError, setUploadError] = useState('');
  const [isDragOver, setIsDragOver] = useState(false);
  const fileInputRef = useRef(null);

  // Asset interaction state
  const [deleteConfirmId, setDeleteConfirmId] = useState(null);
  const [renamingId, setRenamingId] = useState(null);
  const [renameValue, setRenameValue] = useState('');
  const [moveAssetId, setMoveAssetId] = useState(null);

  // Folder state
  const [folderTree, setFolderTree] = useState([]);
  // null = all assets, 0 = unfiled, N = specific folder ID
  const [selectedFolderId, setSelectedFolderId] = useState(null);
  const [filteredAssets, setFilteredAssets] = useState([]);
  const [filteredLoading, setFilteredLoading] = useState(false);
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [renamingFolderId, setRenamingFolderId] = useState(null);
  const [renameFolderValue, setRenameFolderValue] = useState('');
  const [folderDeleteConfirmId, setFolderDeleteConfirmId] = useState(null);
  const [sharingFolderId, setSharingFolderId] = useState(null);
  const [sharingMode, setSharingMode] = useState('read_only');
  const [sharingEnabled, setSharingEnabled] = useState(false);

  // Search and sort state
  const [searchQuery, setSearchQuery] = useState('');
  const [sortField, setSortField] = useState('date');
  const [sortDir, setSortDir] = useState('desc');

  // Breadcrumb state — path from root to current folder
  const [breadcrumbs, setBreadcrumbs] = useState([]);

  /* ── Folder tree fetching ────────────────────────────────────────────── */

  const refreshFolders = useCallback(async () => {
    try { setFolderTree(await getFolderTree()); } catch (e) { console.error('Failed to load folders:', e); }
  }, []);

  useEffect(() => { refreshFolders(); }, [refreshFolders]);

  /* ── Build breadcrumbs from folder tree ───────────────────────────────── */

  useEffect(() => {
    if (selectedFolderId === null || selectedFolderId === 0) {
      setBreadcrumbs([]);
      return;
    }
    // Walk the tree to find the path to the selected folder
    const path = [];
    const findPath = (nodes, target) => {
      for (const node of nodes) {
        if (node.id === target) { path.push({ id: node.id, name: node.name }); return true; }
        if (node.children && findPath(node.children, target)) {
          path.unshift({ id: node.id, name: node.name });
          return true;
        }
      }
      return false;
    };
    findPath(folderTree, selectedFolderId);
    setBreadcrumbs(path);
  }, [selectedFolderId, folderTree]);

  /* ── Filtered asset fetching ─────────────────────────────────────────── */

  const refreshFilteredAssets = useCallback(async () => {
    setFilteredLoading(true);
    try {
      const params = {};
      if (selectedFolderId !== null) params.folderId = selectedFolderId;
      if (searchQuery) params.search = searchQuery;
      if (sortField) params.sort = sortField;
      if (sortDir) params.sortDir = sortDir;
      const d = await listAssets(params);
      setFilteredAssets(d.assets);
    } catch (e) { console.error('Failed to load filtered assets:', e); }
    finally { setFilteredLoading(false); }
  }, [selectedFolderId, searchQuery, sortField, sortDir]);

  useEffect(() => { refreshFilteredAssets(); }, [refreshFilteredAssets]);

  // Re-fetch filtered assets when the parent's asset list changes
  // (e.g., after upload completes or transcode progress updates)
  const prevAssetsRef = useRef(assets);
  useEffect(() => {
    if (prevAssetsRef.current !== assets) {
      prevAssetsRef.current = assets;
      refreshFilteredAssets();
    }
  }, [assets, refreshFilteredAssets]);

  /* ── Upload handlers ─────────────────────────────────────────────────── */

  // Presentation file extensions — routed to the presentation upload API instead of asset upload
  const PRESENTATION_EXTS = new Set(['.pptx', '.ppt', '.odp', '.pdf']);

  const handleFiles = async (files) => {
    setUploadError('');
    for (const file of files) {
      const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
      const isPresentation = PRESENTATION_EXTS.has(ext);
      try {
        setUploadProgress(0);
        if (isPresentation) {
          // Presentation files are converted to slide images, not transcoded as media
          await uploadPresentation(file, (pct) => setUploadProgress(pct));
          setUploadProgress(null);
          setUploadError('Presentation uploaded — select it in a Browser Source stream to use it.');
        } else {
          const newAsset = await uploadAsset(file, (pct) => setUploadProgress(pct));
          setUploadProgress(null);
          // If we're viewing a specific folder, move the new asset into it
          if (selectedFolderId && selectedFolderId > 0) {
            try { await moveAssets([newAsset.id], selectedFolderId); } catch {}
          }
        }
        onRefresh();
      } catch (err) { setUploadError(err.message); setUploadProgress(null); }
    }
  };

  const onDrop = (e) => { e.preventDefault(); setIsDragOver(false); handleFiles(Array.from(e.dataTransfer.files)); };
  const onFileSelect = (e) => { handleFiles(Array.from(e.target.files)); e.target.value = ''; };

  /* ── Asset action handlers ───────────────────────────────────────────── */

  const doDelete = async (assetId) => {
    try { await deleteAsset(assetId); setDeleteConfirmId(null); onRefresh(); onRefreshStreams(); }
    catch (err) { console.error('Delete failed:', err); }
  };

  const doAddToStream = async (assetId) => {
    if (!selectedStreamId) return;
    try { await addPlaylistItem(selectedStreamId, assetId); onRefreshStreams(); }
    catch (err) { console.error('Add to stream failed:', err); }
  };

  const startRename = (asset) => { setRenamingId(asset.id); setRenameValue(asset.display_name); };
  const doRename = async (assetId) => {
    if (!renameValue.trim()) { setRenamingId(null); return; }
    try { await renameAsset(assetId, renameValue.trim()); setRenamingId(null); onRefresh(); onRefreshStreams(); }
    catch (err) { console.error('Rename failed:', err); }
  };
  const handleRenameKey = (e, assetId) => {
    if (e.key === 'Enter') doRename(assetId);
    if (e.key === 'Escape') setRenamingId(null);
  };

  const doMoveAsset = async (assetId, targetFolderId) => {
    try {
      await moveAssets([assetId], targetFolderId === 'unfiled' ? null : parseInt(targetFolderId));
      setMoveAssetId(null);
      onRefresh();
    } catch (err) { console.error('Move failed:', err); }
  };

  /* ── Folder action handlers ──────────────────────────────────────────── */

  const doCreateFolder = async () => {
    if (!newFolderName.trim()) { setCreatingFolder(false); return; }
    try {
      const parentId = selectedFolderId && selectedFolderId > 0 ? selectedFolderId : null;
      await createFolder(newFolderName.trim(), parentId);
      setNewFolderName('');
      setCreatingFolder(false);
      refreshFolders();
    } catch (err) { console.error('Create folder failed:', err); }
  };

  const doRenameFolder = async (folderId) => {
    if (!renameFolderValue.trim()) { setRenamingFolderId(null); return; }
    try {
      await updateFolder(folderId, { name: renameFolderValue.trim() });
      setRenamingFolderId(null);
      refreshFolders();
    } catch (err) { console.error('Rename folder failed:', err); }
  };

  const doDeleteFolder = async (folderId) => {
    try {
      await deleteFolder(folderId);
      setFolderDeleteConfirmId(null);
      if (selectedFolderId === folderId) setSelectedFolderId(null);
      refreshFolders();
      onRefresh();
    } catch (err) { console.error('Delete folder failed:', err); }
  };

  const doUpdateSharing = async (folderId) => {
    try {
      await updateFolderSharing(folderId, sharingEnabled, sharingMode);
      setSharingFolderId(null);
      refreshFolders();
    } catch (err) { console.error('Share update failed:', err); }
  };

  const openSharingDialog = (folder) => {
    setSharingFolderId(folder.id);
    setSharingEnabled(folder.is_shared);
    setSharingMode(folder.share_mode || 'read_only');
  };

  /* ── Find selected folder info ───────────────────────────────────────── */

  const findFolder = (nodes, id) => {
    for (const node of nodes) {
      if (node.id === id) return node;
      if (node.children) {
        const found = findFolder(node.children, id);
        if (found) return found;
      }
    }
    return null;
  };

  const selectedFolder = selectedFolderId && selectedFolderId > 0
    ? findFolder(folderTree, selectedFolderId)
    : null;

  /* ── Flatten folder tree for the move dropdown ───────────────────────── */

  const flattenFolders = (nodes, depth = 0) => {
    let result = [];
    for (const node of nodes) {
      result.push({ id: node.id, name: node.name, depth });
      if (node.children) result = result.concat(flattenFolders(node.children, depth + 1));
    }
    return result;
  };
  const flatFolders = flattenFolders(folderTree);

  /* ── Which assets to display ─────────────────────────────────────────── */

  const displayAssets = filteredAssets.length > 0 || selectedFolderId !== null || searchQuery
    ? filteredAssets
    : assets;
  const isLoadingDisplay = selectedFolderId !== null || searchQuery ? filteredLoading : isLoading;

  /* ── Render ──────────────────────────────────────────────────────────── */

  return (
    <div className="asset-library-container">
      {/* Folder sidebar */}
      <div className="folder-sidebar">
        <div className="folder-sidebar-header">
          <span className="folder-sidebar-title">Folders</span>
          <button className="btn btn-xs btn-accent" onClick={() => setCreatingFolder(true)}
            title="New folder">+</button>
        </div>

        {/* New folder input */}
        {creatingFolder && (
          <div className="folder-create-row">
            <input
              className="folder-create-input"
              type="text"
              placeholder="Folder name"
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') doCreateFolder(); if (e.key === 'Escape') setCreatingFolder(false); }}
              autoFocus
            />
          </div>
        )}

        {/* "All Assets" and "Unfiled" special entries */}
        <div
          className={`folder-tree-item ${selectedFolderId === null ? 'folder-tree-item-active' : ''}`}
          onClick={() => setSelectedFolderId(null)}
        >
          <span className="folder-expand"> </span>
          <span className="folder-icon">🗂</span>
          <span className="folder-tree-name">All Assets</span>
        </div>
        <div
          className={`folder-tree-item ${selectedFolderId === 0 ? 'folder-tree-item-active' : ''}`}
          onClick={() => setSelectedFolderId(0)}
        >
          <span className="folder-expand"> </span>
          <span className="folder-icon">📄</span>
          <span className="folder-tree-name">Unfiled</span>
        </div>

        {/* Folder tree */}
        <div className="folder-tree">
          {folderTree.map(folder => (
            <FolderTreeNode
              key={folder.id} folder={folder}
              selectedFolderId={selectedFolderId}
              onSelect={setSelectedFolderId}
              isAdmin={isAdmin}
            />
          ))}
        </div>
      </div>

      {/* Main content area */}
      <div className="asset-library">
        <div className="panel-header">
          <div className="panel-header-left">
            <h2>Media Library</h2>
            {/* Breadcrumbs */}
            {breadcrumbs.length > 0 && (
              <div className="breadcrumbs">
                <span className="breadcrumb-item breadcrumb-link" onClick={() => setSelectedFolderId(null)}>All</span>
                {breadcrumbs.map((bc, i) => (
                  <React.Fragment key={bc.id}>
                    <span className="breadcrumb-sep">/</span>
                    <span
                      className={`breadcrumb-item ${i === breadcrumbs.length - 1 ? 'breadcrumb-current' : 'breadcrumb-link'}`}
                      onClick={() => { if (i < breadcrumbs.length - 1) setSelectedFolderId(bc.id); }}
                    >{bc.name}</span>
                  </React.Fragment>
                ))}
              </div>
            )}
          </div>
          <span className="panel-count">{displayAssets.length} assets</span>
        </div>

        {/* Folder actions bar — shown when a specific folder is selected */}
        {selectedFolder && (
          <div className="folder-actions-bar">
            <span className="folder-actions-name">
              {renamingFolderId === selectedFolder.id ? (
                <input
                  className="rename-input"
                  type="text"
                  value={renameFolderValue}
                  onChange={(e) => setRenameFolderValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') doRenameFolder(selectedFolder.id);
                    if (e.key === 'Escape') setRenamingFolderId(null);
                  }}
                  onBlur={() => doRenameFolder(selectedFolder.id)}
                  autoFocus
                />
              ) : (
                <span
                  className="folder-name-editable"
                  onClick={() => { setRenamingFolderId(selectedFolder.id); setRenameFolderValue(selectedFolder.name); }}
                  title="Click to rename"
                >{selectedFolder.name}</span>
              )}
              {selectedFolder.is_shared && (
                <span className="badge badge-sm badge-info">
                  {selectedFolder.share_mode === 'read_write' ? 'shared r/w' : 'shared r/o'}
                </span>
              )}
            </span>
            <div className="folder-actions-buttons">
              {isAdmin && (
                <button className="btn btn-xs btn-accent" onClick={() => openSharingDialog(selectedFolder)}>
                  Share
                </button>
              )}
              {folderDeleteConfirmId === selectedFolder.id ? (
                <div className="delete-confirm">
                  <button className="btn btn-xs btn-danger" onClick={() => doDeleteFolder(selectedFolder.id)}>Confirm</button>
                  <button className="btn btn-xs btn-ghost" onClick={() => setFolderDeleteConfirmId(null)}>Cancel</button>
                </div>
              ) : (
                <button className="btn btn-xs btn-ghost btn-delete"
                  onClick={() => setFolderDeleteConfirmId(selectedFolder.id)} title="Delete folder">✕</button>
              )}
            </div>
          </div>
        )}

        {/* Sharing dialog (inline) */}
        {sharingFolderId && (
          <div className="sharing-dialog">
            <div className="sharing-dialog-header">
              <span>Folder Sharing</span>
              <button className="btn btn-xs btn-ghost" onClick={() => setSharingFolderId(null)}>✕</button>
            </div>
            <div className="sharing-dialog-body">
              <label className="sharing-toggle">
                <input type="checkbox" checked={sharingEnabled}
                  onChange={(e) => setSharingEnabled(e.target.checked)} />
                <span>Share with all users</span>
              </label>
              {sharingEnabled && (
                <div className="sharing-mode-select">
                  <label className="form-group">
                    <span>Access level</span>
                    <select value={sharingMode} onChange={(e) => setSharingMode(e.target.value)}>
                      <option value="read_only">Read Only — can view and use assets</option>
                      <option value="read_write">Read/Write — can add, remove, rename assets</option>
                    </select>
                  </label>
                </div>
              )}
            </div>
            <div className="sharing-dialog-footer">
              <button className="btn btn-sm btn-primary" onClick={() => doUpdateSharing(sharingFolderId)}>Save</button>
              <button className="btn btn-sm btn-ghost" onClick={() => setSharingFolderId(null)}>Cancel</button>
            </div>
          </div>
        )}

        {/* Search and sort controls */}
        <div className="asset-toolbar">
          <div className="search-box">
            <input
              type="text"
              placeholder="Search assets..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="search-input"
            />
            {searchQuery && (
              <button className="search-clear" onClick={() => setSearchQuery('')}>✕</button>
            )}
          </div>
          <div className="sort-controls">
            <select value={sortField} onChange={(e) => setSortField(e.target.value)} className="sort-select">
              <option value="date">Date</option>
              <option value="name">Name</option>
              <option value="size">Size</option>
              <option value="type">Type</option>
            </select>
            <button
              className="btn btn-xs btn-ghost sort-dir-btn"
              onClick={() => setSortDir(d => d === 'asc' ? 'desc' : 'asc')}
              title={sortDir === 'asc' ? 'Ascending' : 'Descending'}
            >
              {sortDir === 'asc' ? '↑' : '↓'}
            </button>
          </div>
        </div>

        {/* Storage bar */}
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

        {/* Upload zone */}
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

        {/* Asset grid */}
        {isLoadingDisplay ? <div className="loading-state">Loading library...</div>
         : displayAssets.length === 0 ? (
           <div className="empty-state">
             {searchQuery ? 'No assets match your search.' :
              selectedFolderId === 0 ? 'No unfiled assets.' :
              selectedFolderId ? 'This folder is empty.' :
              'No assets yet. Upload a video or image to get started.'}
           </div>
         ) : (
          <div className="asset-grid">
            {displayAssets.map((asset) => {
              const badge = STATUS_BADGE[asset.status] || STATUS_BADGE.error;
              const isReady = asset.status === 'ready';
              const isRenaming = renamingId === asset.id;
              const isMoving = moveAssetId === asset.id;
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
                      {/* Show folder name when viewing all assets */}
                      {selectedFolderId === null && asset.folder_name && (
                        <>
                          <span className="meta-dot">·</span>
                          <span className="asset-folder-tag" onClick={() => setSelectedFolderId(asset.folder_id)}
                            title="Go to folder">{asset.folder_name}</span>
                        </>
                      )}
                    </div>
                  </div>
                  <div className="asset-actions">
                    {/* Move to folder */}
                    {isMoving ? (
                      <select
                        className="move-folder-select"
                        value=""
                        onChange={(e) => doMoveAsset(asset.id, e.target.value)}
                        onBlur={() => setMoveAssetId(null)}
                        autoFocus
                      >
                        <option value="" disabled>Move to...</option>
                        <option value="unfiled">Unfiled</option>
                        {flatFolders.map(f => (
                          <option key={f.id} value={f.id}>
                            {'  '.repeat(f.depth) + f.name}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <button className="btn btn-xs btn-ghost" onClick={() => setMoveAssetId(asset.id)}
                        title="Move to folder">📁</button>
                    )}
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
    </div>
  );
}
