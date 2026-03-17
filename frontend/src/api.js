/**
 * api.js — Centralized API client for all backend communication.
 *
 * Responsibilities:
 *   - JWT token persistence in localStorage (survives page refreshes and new tabs)
 *   - Automatic Authorization header injection on every request
 *   - Global 401 handling: clears the token and fires a custom 'auth-expired' DOM event
 *     so App.jsx can force a sign-out regardless of which component triggered the request
 *   - Content-Type negotiation: JSON by default, omitted for FormData (browser sets boundary)
 *
 * Upload uses XMLHttpRequest instead of fetch() because fetch does not expose upload
 * progress events — we need those for the real-time upload percentage bar in AssetLibrary.
 */
const API_BASE = '/api';
/** localStorage key for the JWT access token */
const TOKEN_KEY = 'mcs_access_token';

// --- Token helpers ---
// These are deliberately simple wrappers so that the storage key is defined in one place.
export function getStoredToken() { return localStorage.getItem(TOKEN_KEY); }
export function setStoredToken(token) { localStorage.setItem(TOKEN_KEY, token); }
export function clearStoredToken() { localStorage.removeItem(TOKEN_KEY); }

/**
 * Core fetch wrapper used by all API functions (except uploadAsset which needs XHR).
 *
 * Handles:
 *   - Attaching the JWT Bearer token if one exists
 *   - Setting Content-Type to JSON unless the body is FormData
 *   - 401 → clear token + dispatch 'auth-expired' event for global sign-out
 *   - Non-ok responses → extract `detail` from the JSON error body if available
 *   - 204 No Content → return null instead of trying to parse empty JSON
 */
async function apiFetch(path, options = {}) {
  const token = getStoredToken();
  const headers = { ...(options.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  // Let the browser set the multipart boundary automatically for FormData uploads
  if (!(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';

  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (response.status === 401) {
    // Token is expired or invalid — force a global sign-out via custom DOM event
    clearStoredToken();
    window.dispatchEvent(new Event('auth-expired'));
    throw new Error('Authentication expired');
  }
  if (!response.ok) {
    // Try to extract a human-readable error message from the FastAPI error response
    let msg = `Request failed: ${response.status}`;
    try { const body = await response.json(); msg = body.detail || msg; } catch {}
    throw new Error(msg);
  }
  // DELETE endpoints return 204 with no body
  if (response.status === 204) return null;
  return response.json();
}

// ─── Auth ─────────────────────────────────────────────────────────────────────

/**
 * Authenticates with username/password and stores the returned JWT.
 * Returns the full login response which includes must_change_password flag.
 */
export async function login(username, password) {
  const data = await apiFetch('/auth/login', {
    method: 'POST', body: JSON.stringify({ username, password }),
  });
  // Persist the token immediately so subsequent API calls are authenticated
  setStoredToken(data.access_token);
  return data;
}

/** Fetches the current user's profile (username, is_admin, must_change_password). */
export async function getCurrentUser() { return apiFetch('/auth/me'); }

/** Client-side logout — simply clears the stored JWT (no server-side session to invalidate). */
export function logout() { clearStoredToken(); }

/** Changes the current user's password. Server validates current_password before accepting. */
export async function changePassword(currentPassword, newPassword) {
  return apiFetch('/auth/change-password', {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}

// ─── Assets ───────────────────────────────────────────────────────────────────

/**
 * Uploads a media file using XMLHttpRequest for real-time progress tracking.
 *
 * We can't use fetch() here because the Fetch API doesn't expose upload progress events.
 * The onProgress callback receives a percentage (0-100) on each progress event so the
 * UI can render a live upload bar.
 *
 * After upload completes, the backend kicks off async transcoding (H.264/AAC normalization).
 * The asset will have status='processing' until transcoding finishes.
 */
export async function uploadAsset(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const formData = new FormData();
    formData.append('file', file);
    if (onProgress) {
      // Track upload progress via the XHR upload event (not available on fetch)
      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      });
    }
    xhr.addEventListener('load', () => {
      if (xhr.status === 201) resolve(JSON.parse(xhr.responseText));
      else { try { reject(new Error(JSON.parse(xhr.responseText).detail)); } catch { reject(new Error(`Upload failed: ${xhr.status}`)); } }
    });
    xhr.addEventListener('error', () => reject(new Error('Network error')));
    const token = getStoredToken();
    xhr.open('POST', `${API_BASE}/assets/upload`);
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    xhr.send(formData);
  });
}

/** Lists all assets visible to the current user (admin sees all, users see only their own). */
export async function listAssets() { return apiFetch('/assets'); }
export async function getAsset(id) { return apiFetch(`/assets/${id}`); }
export async function deleteAsset(id) { return apiFetch(`/assets/${id}`, { method: 'DELETE' }); }
/** Renames the display name of an asset (does not change the underlying filename on disk). */
export async function renameAsset(id, displayName) {
  return apiFetch(`/assets/${id}/rename`, {
    method: 'PUT', body: JSON.stringify({ display_name: displayName }),
  });
}

// ─── Streams ──────────────────────────────────────────────────────────────────

/** Creates a new stream. Admin-only. source_type is 'playlist' or 'browser'. */
export async function createStream(data) { return apiFetch('/streams', { method: 'POST', body: JSON.stringify(data) }); }
/** Lists all streams (admin sees all, regular users see only their assigned channels). */
export async function listStreams() { return apiFetch('/streams'); }
export async function getStream(id) { return apiFetch(`/streams/${id}`); }
/** Updates stream configuration (name, multicast address/port, playback mode). Admin-only. */
export async function updateStream(id, data) { return apiFetch(`/streams/${id}`, { method: 'PUT', body: JSON.stringify(data) }); }
export async function deleteStream(id) { return apiFetch(`/streams/${id}`, { method: 'DELETE' }); }
/** Updates the browser source configuration (URL and audio capture toggle) for a browser-type stream. */
export async function updateBrowserConfig(streamId, url, captureAudio) {
  return apiFetch(`/streams/${streamId}/browser`, {
    method: 'PUT', body: JSON.stringify({ url, capture_audio: captureAudio }),
  });
}

// ─── Playlist ─────────────────────────────────────────────────────────────────

/** Appends an asset to the end of a stream's playlist. */
export async function addPlaylistItem(streamId, assetId) {
  return apiFetch(`/streams/${streamId}/items`, { method: 'POST', body: JSON.stringify({ asset_id: assetId }) });
}
/** Reorders the playlist by providing the complete ordered list of asset IDs. */
export async function reorderPlaylist(streamId, assetIds) {
  return apiFetch(`/streams/${streamId}/items/reorder`, { method: 'PUT', body: JSON.stringify({ asset_ids: assetIds }) });
}
export async function removePlaylistItem(streamId, itemId) {
  return apiFetch(`/streams/${streamId}/items/${itemId}`, { method: 'DELETE' });
}

// ─── Playback control ─────────────────────────────────────────────────────────
// These dispatch to stream_manager (playlist) or browser_manager (browser source)
// on the backend depending on the stream's source_type.

export async function startStream(id) { return apiFetch(`/streams/${id}/start`, { method: 'POST' }); }
export async function stopStream(id) { return apiFetch(`/streams/${id}/stop`, { method: 'POST' }); }
/** Stops and immediately restarts the stream (useful after playlist or config changes). */
export async function restartStream(id) { return apiFetch(`/streams/${id}/restart`, { method: 'POST' }); }
/** Fetches live status including ffmpeg PID and runtime info for the transport controls display. */
export async function getStreamStatus(id) { return apiFetch(`/streams/${id}/status`); }

// ─── Settings ─────────────────────────────────────────────────────────────────

/** Fetches all server settings (13 runtime-adjustable values). Admin-only. */
export async function getSettings() { return apiFetch('/settings'); }
/** Batch-updates server settings. Only changed keys need to be included in the map. */
export async function updateSettings(settingsMap) {
  return apiFetch('/settings', { method: 'PUT', body: JSON.stringify({ settings: settingsMap }) });
}

// ─── User Management ─────────────────────────────────────────────────────────
// All user management endpoints are admin-only.

export async function listUsers() { return apiFetch('/auth/users'); }
/**
 * Creates a new user. The backend generates a random password and returns it in the response.
 * The admin must share this password with the user — it's only shown once.
 */
export async function createUser(username, isAdmin = false) {
  return apiFetch('/auth/users', { method: 'POST', body: JSON.stringify({ username, is_admin: isAdmin }) });
}
/** Updates user properties (is_admin, is_active). */
export async function updateUser(userId, data) {
  return apiFetch(`/auth/users/${userId}`, { method: 'PUT', body: JSON.stringify(data) });
}
/** Resets a user's password to a new random value. Returns the new password (shown once). */
export async function resetUserPassword(userId) {
  return apiFetch(`/auth/users/${userId}/reset-password`, { method: 'POST' });
}
export async function deleteUser(userId) {
  return apiFetch(`/auth/users/${userId}`, { method: 'DELETE' });
}
/**
 * Assigns a list of user IDs to a stream for RBAC access control.
 * Replaces the entire assignment list for that stream (not additive).
 */
export async function assignStreamUsers(streamId, userIds) {
  return apiFetch(`/streams/${streamId}/assign`, { method: 'PUT', body: JSON.stringify({ user_ids: userIds }) });
}

// ─── Storage ──────────────────────────────────────────────────────────────────

/** Fetches disk usage stats for the media storage directory. */
export async function getStorageInfo() { return apiFetch('/assets/storage'); }

// ─── Monitoring ───────────────────────────────────────────────────────────────

/** Fetches system resource metrics: CPU, RAM, network, per-stream breakdown, capacity estimates. */
export async function getMonitoring() { return apiFetch('/monitoring'); }
