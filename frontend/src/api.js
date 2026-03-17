/**
 * API client — centralized HTTP calls with JWT token management.
 * Token stored in localStorage for persistence across tabs/refreshes.
 */
const API_BASE = '/api';
const TOKEN_KEY = 'mcs_access_token';

export function getStoredToken() { return localStorage.getItem(TOKEN_KEY); }
export function setStoredToken(token) { localStorage.setItem(TOKEN_KEY, token); }
export function clearStoredToken() { localStorage.removeItem(TOKEN_KEY); }

async function apiFetch(path, options = {}) {
  const token = getStoredToken();
  const headers = { ...(options.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (!(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';

  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (response.status === 401) {
    clearStoredToken();
    window.dispatchEvent(new Event('auth-expired'));
    throw new Error('Authentication expired');
  }
  if (!response.ok) {
    let msg = `Request failed: ${response.status}`;
    try { const body = await response.json(); msg = body.detail || msg; } catch {}
    throw new Error(msg);
  }
  if (response.status === 204) return null;
  return response.json();
}

// --- Auth ---
export async function login(username, password) {
  const data = await apiFetch('/auth/login', {
    method: 'POST', body: JSON.stringify({ username, password }),
  });
  setStoredToken(data.access_token);
  return data;
}
export async function getCurrentUser() { return apiFetch('/auth/me'); }
export function logout() { clearStoredToken(); }
export async function changePassword(currentPassword, newPassword) {
  return apiFetch('/auth/change-password', {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}

// --- Assets ---
export async function uploadAsset(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const formData = new FormData();
    formData.append('file', file);
    if (onProgress) {
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
export async function listAssets() { return apiFetch('/assets'); }
export async function getAsset(id) { return apiFetch(`/assets/${id}`); }
export async function deleteAsset(id) { return apiFetch(`/assets/${id}`, { method: 'DELETE' }); }
export async function renameAsset(id, displayName) {
  return apiFetch(`/assets/${id}/rename`, {
    method: 'PUT', body: JSON.stringify({ display_name: displayName }),
  });
}

// --- Streams ---
export async function createStream(data) { return apiFetch('/streams', { method: 'POST', body: JSON.stringify(data) }); }
export async function listStreams() { return apiFetch('/streams'); }
export async function getStream(id) { return apiFetch(`/streams/${id}`); }
export async function updateStream(id, data) { return apiFetch(`/streams/${id}`, { method: 'PUT', body: JSON.stringify(data) }); }
export async function deleteStream(id) { return apiFetch(`/streams/${id}`, { method: 'DELETE' }); }
export async function updateBrowserConfig(streamId, url, captureAudio) {
  return apiFetch(`/streams/${streamId}/browser`, {
    method: 'PUT', body: JSON.stringify({ url, capture_audio: captureAudio }),
  });
}

// --- Playlist ---
export async function addPlaylistItem(streamId, assetId) {
  return apiFetch(`/streams/${streamId}/items`, { method: 'POST', body: JSON.stringify({ asset_id: assetId }) });
}
export async function reorderPlaylist(streamId, assetIds) {
  return apiFetch(`/streams/${streamId}/items/reorder`, { method: 'PUT', body: JSON.stringify({ asset_ids: assetIds }) });
}
export async function removePlaylistItem(streamId, itemId) {
  return apiFetch(`/streams/${streamId}/items/${itemId}`, { method: 'DELETE' });
}

// --- Playback control ---
export async function startStream(id) { return apiFetch(`/streams/${id}/start`, { method: 'POST' }); }
export async function stopStream(id) { return apiFetch(`/streams/${id}/stop`, { method: 'POST' }); }
export async function restartStream(id) { return apiFetch(`/streams/${id}/restart`, { method: 'POST' }); }
export async function getStreamStatus(id) { return apiFetch(`/streams/${id}/status`); }

// --- Settings ---
export async function getSettings() { return apiFetch('/settings'); }
export async function updateSettings(settingsMap) {
  return apiFetch('/settings', { method: 'PUT', body: JSON.stringify({ settings: settingsMap }) });
}

// --- User Management ---
export async function listUsers() { return apiFetch('/auth/users'); }
export async function createUser(username, isAdmin = false) {
  return apiFetch('/auth/users', { method: 'POST', body: JSON.stringify({ username, is_admin: isAdmin }) });
}
export async function updateUser(userId, data) {
  return apiFetch(`/auth/users/${userId}`, { method: 'PUT', body: JSON.stringify(data) });
}
export async function resetUserPassword(userId) {
  return apiFetch(`/auth/users/${userId}/reset-password`, { method: 'POST' });
}
export async function deleteUser(userId) {
  return apiFetch(`/auth/users/${userId}`, { method: 'DELETE' });
}
export async function assignStreamUsers(streamId, userIds) {
  return apiFetch(`/streams/${streamId}/assign`, { method: 'PUT', body: JSON.stringify({ user_ids: userIds }) });
}

// --- Storage ---
export async function getStorageInfo() { return apiFetch('/assets/storage'); }

// --- Monitoring ---
export async function getMonitoring() { return apiFetch('/monitoring'); }
