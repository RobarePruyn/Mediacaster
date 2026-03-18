/**
 * Settings.jsx — Settings panel with three tabs (admin sees all, regular users see Account only).
 *
 * Tabs:
 *   1. User Management (admin): Create users, toggle admin/active status, reset passwords,
 *      delete users, and assign stream channel access (RBAC).
 *   2. Server Settings (admin): Runtime-adjustable server configuration organized by category
 *      (stream limits, transcode profile, media defaults, multicast defaults).
 *   3. Account (all users): View account info and change password.
 *
 * Key design decisions:
 *   - User creation generates a random server-side password that's displayed once.
 *     The admin must copy and share it immediately — it cannot be retrieved later.
 *   - Stream assignment uses a modal overlay with checkboxes. Saving iterates over all
 *     streams and issues individual assignStreamUsers() calls for any that changed,
 *     because the API expects the full user list per stream (not per user).
 *   - Server settings only sends changed values to the API (not the entire settings map).
 */
import React, { useState, useEffect } from 'react';
import {
  getSettings, updateSettings, changePassword,
  listUsers, createUser, updateUser, resetUserPassword, deleteUser,
  listStreams, assignStreamUsers,
} from '../api';

export default function Settings({ currentUser }) {
  const isAdmin = currentUser?.is_admin;
  /** Active settings tab — admin defaults to 'users', regular users default to 'account' */
  const [activeTab, setActiveTab] = useState(isAdmin ? 'users' : 'account');

  // ─── Server settings state ────────────────────────────────────────────────
  /** Array of setting objects from the API (each has key, value, description) */
  const [settings, setSettings] = useState([]);
  /** Editable copy of settings values keyed by setting key (for detecting changes on save) */
  const [editValues, setEditValues] = useState({});
  const [settingsLoading, setSettingsLoading] = useState(true);
  /** Status message shown after save attempt (e.g., "Saved 3 setting(s)" or "Error: ...") */
  const [saveStatus, setSaveStatus] = useState('');

  // ─── User management state ────────────────────────────────────────────────
  const [users, setUsers] = useState([]);
  const [usersLoading, setUsersLoading] = useState(true);
  const [newUsername, setNewUsername] = useState('');
  const [newIsAdmin, setNewIsAdmin] = useState(false);
  /** Holds the response from createUser() (includes the generated password shown once) */
  const [createdUserInfo, setCreatedUserInfo] = useState(null);
  /** Holds the response from resetUserPassword() (includes the new password shown once) */
  const [resetInfo, setResetInfo] = useState(null);
  const [userError, setUserError] = useState('');

  // ─── Stream assignment state ──────────────────────────────────────────────
  /** All streams (fetched for the channel assignment modal) */
  const [streams, setStreams] = useState([]);
  /** User ID currently being edited in the assignment modal, or null if modal is closed */
  const [selectedUserId, setSelectedUserId] = useState(null);
  /** Working copy of stream IDs assigned to the selected user (modified by checkbox toggles) */
  const [assignedStreamIds, setAssignedStreamIds] = useState([]);

  // ─── Password change state ────────────────────────────────────────────────
  const [currentPw, setCurrentPw] = useState('');
  const [newPw, setNewPw] = useState('');
  const [confirmPw, setConfirmPw] = useState('');
  const [pwError, setPwError] = useState('');
  const [pwSuccess, setPwSuccess] = useState('');
  const [pwLoading, setPwLoading] = useState(false);

  // Load all admin data on mount (only if user is admin)
  useEffect(() => { if (isAdmin) { loadSettings(); loadUsers(); loadStreams(); } }, [isAdmin]);

  /** Fetches server settings and populates both the display array and editable values map. */
  const loadSettings = async () => {
    try {
      const data = await getSettings();
      setSettings(data);
      // Build a key→value map for the editable inputs
      const vals = {};
      data.forEach(s => { vals[s.key] = s.value; });
      setEditValues(vals);
    } catch (err) { console.error(err); }
    finally { setSettingsLoading(false); }
  };

  const loadUsers = async () => {
    try { const d = await listUsers(); setUsers(d.users); }
    catch (err) { console.error(err); }
    finally { setUsersLoading(false); }
  };

  /** Fetches streams for the channel assignment modal. */
  const loadStreams = async () => {
    try { const d = await listStreams(); setStreams(d.streams); }
    catch (err) { console.error(err); }
  };

  /**
   * Saves only the settings that have changed (compares editValues against original values).
   * This avoids unnecessary server-side processing and makes the save status message accurate.
   */
  const handleSaveSettings = async () => {
    setSaveStatus('');
    const changed = {};
    settings.forEach(s => { if (editValues[s.key] !== s.value) changed[s.key] = editValues[s.key]; });
    if (Object.keys(changed).length === 0) { setSaveStatus('No changes'); return; }
    try { await updateSettings(changed); setSaveStatus(`Saved ${Object.keys(changed).length} setting(s)`); loadSettings(); }
    catch (err) { setSaveStatus(`Error: ${err.message}`); }
  };

  /**
   * Creates a new user. The backend generates a random password and returns it.
   * We display it in createdUserInfo — the admin must copy it immediately since
   * it's never shown again (passwords are stored as bcrypt hashes).
   */
  const handleCreateUser = async () => {
    setUserError(''); setCreatedUserInfo(null);
    if (!newUsername.trim()) { setUserError('Username required'); return; }
    try {
      const result = await createUser(newUsername.trim(), newIsAdmin);
      setCreatedUserInfo(result);
      setNewUsername(''); setNewIsAdmin(false);
      loadUsers();
    } catch (err) { setUserError(err.message); }
  };

  /** Toggles a user's admin status. */
  const handleToggleAdmin = async (userId, currentIsAdmin) => {
    try { await updateUser(userId, { is_admin: !currentIsAdmin }); loadUsers(); }
    catch (err) { setUserError(err.message); }
  };

  /** Toggles a user's active status (disabled users cannot log in). */
  const handleToggleActive = async (userId, currentIsActive) => {
    try { await updateUser(userId, { is_active: !currentIsActive }); loadUsers(); }
    catch (err) { setUserError(err.message); }
  };

  /** Resets a user's password to a new random value. Displays the new password once. */
  const handleResetPassword = async (userId) => {
    setResetInfo(null);
    try { const r = await resetUserPassword(userId); setResetInfo({ userId, ...r }); loadUsers(); }
    catch (err) { setUserError(err.message); }
  };

  /** Deletes a user after confirmation. Their uploaded assets remain but become unowned. */
  const handleDeleteUser = async (userId) => {
    if (!window.confirm('Delete this user? Their assets will remain but become unowned.')) return;
    try { await deleteUser(userId); loadUsers(); }
    catch (err) { setUserError(err.message); }
  };

  /**
   * Opens the channel assignment modal for a user.
   * Pre-populates the checkbox state from the user's current assigned_stream_ids.
   */
  const handleSelectUserForAssign = (userId) => {
    setSelectedUserId(userId);
    const user = users.find(u => u.id === userId);
    setAssignedStreamIds(user?.assigned_stream_ids || []);
  };

  /** Toggles a stream's assignment in the working copy (does not save until Save is clicked). */
  const handleToggleStreamAssign = (streamId) => {
    setAssignedStreamIds(prev =>
      prev.includes(streamId) ? prev.filter(id => id !== streamId) : [...prev, streamId]
    );
  };

  /**
   * Saves channel assignments by iterating over all streams and updating any that changed.
   * The API expects the full list of user IDs per stream (not per user), so we need to
   * diff the old and new state for each stream and send individual update requests.
   */
  const handleSaveAssignments = async () => {
    for (const stream of streams) {
      const currentAssigned = stream.assigned_user_ids || [];
      const shouldBeAssigned = assignedStreamIds.includes(stream.id);
      const isCurrentlyAssigned = currentAssigned.includes(selectedUserId);

      // Only make an API call if this stream's assignment actually changed
      if (shouldBeAssigned !== isCurrentlyAssigned) {
        let newUserIds;
        if (shouldBeAssigned) {
          newUserIds = [...currentAssigned, selectedUserId];
        } else {
          newUserIds = currentAssigned.filter(id => id !== selectedUserId);
        }
        await assignStreamUsers(stream.id, newUserIds);
      }
    }
    // Refresh both users and streams to reflect the new assignments
    loadUsers(); loadStreams();
    setSelectedUserId(null);
  };

  /** Handles the Account tab's password change form. Client-side validation first. */
  const handleChangePassword = async (e) => {
    e.preventDefault();
    setPwError(''); setPwSuccess('');
    if (newPw.length < 8) { setPwError('Minimum 8 characters'); return; }
    if (newPw !== confirmPw) { setPwError('Passwords do not match'); return; }
    setPwLoading(true);
    try { await changePassword(currentPw, newPw); setPwSuccess('Password changed'); setCurrentPw(''); setNewPw(''); setConfirmPw(''); }
    catch (err) { setPwError(err.message); }
    finally { setPwLoading(false); }
  };

  /**
   * Organizes server settings into display categories.
   * Each category maps to a group of setting keys shown under a shared heading.
   */
  const settingCategories = {
    'Stream Limits': ['max_concurrent_streams', 'max_cpu_utilization', 'max_bandwidth_utilization'],
    'Transcode Profile': ['transcode_resolution', 'transcode_framerate', 'transcode_video_bitrate',
                          'transcode_audio_bitrate', 'transcode_video_preset', 'transcode_video_profile'],
    'Media Defaults': ['static_image_duration'],
    'Multicast Defaults': ['default_multicast_address', 'default_multicast_port', 'multicast_ttl'],
    'Single Sign-On (OIDC)': ['oidc_enabled', 'oidc_discovery_url', 'oidc_client_id',
                               'oidc_client_secret', 'oidc_display_name'],
  };

  // Settings that need special input types
  const dropdownSettings = {
    'oidc_enabled': ['true', 'false'],
  };
  const passwordSettings = ['oidc_client_secret'];

  return (
    <div className="settings-panel">
      {/* Tab bar — admin sees all three tabs, regular users only see Account */}
      <div className="settings-tabs">
        {isAdmin && <button className={`settings-tab ${activeTab === 'users' ? 'active' : ''}`}
          onClick={() => setActiveTab('users')}>User Management</button>}
        {isAdmin && <button className={`settings-tab ${activeTab === 'server' ? 'active' : ''}`}
          onClick={() => setActiveTab('server')}>Server Settings</button>}
        <button className={`settings-tab ${activeTab === 'account' ? 'active' : ''}`}
          onClick={() => setActiveTab('account')}>Account</button>
      </div>

      {/* ═══ User Management Tab ═══════════════════════════════════════════════ */}
      {activeTab === 'users' && isAdmin && (
        <div className="settings-content">
          {/* Create user section */}
          <div className="settings-group">
            <h3 className="settings-group-title">Create User</h3>
            {userError && <div className="login-error">{userError}</div>}
            {/* Show the generated password after successful creation (one-time display) */}
            {createdUserInfo && (
              <div className="settings-success">
                User <strong>{createdUserInfo.user.username}</strong> created.
                Generated password: <code className="mono">{createdUserInfo.generated_password}</code>
                <br /><em>Copy this password now — it won't be shown again.</em>
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
              <div className="form-group" style={{ flex: 1 }}>
                <label>Username</label>
                <input type="text" value={newUsername} onChange={e => setNewUsername(e.target.value)}
                  placeholder="e.g. jsmith" />
              </div>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12,
                color: 'var(--text-secondary)', padding: '8px 0' }}>
                <input type="checkbox" checked={newIsAdmin} onChange={e => setNewIsAdmin(e.target.checked)} />
                Admin
              </label>
              <button className="btn btn-primary" onClick={handleCreateUser}
                style={{ marginBottom: 4 }}>Create</button>
            </div>
          </div>

          {/* User list section */}
          <div className="settings-group">
            <h3 className="settings-group-title">Users ({users.length})</h3>
            {/* Show the new password after a password reset (one-time display) */}
            {resetInfo && (
              <div className="settings-success">
                Password reset. New password: <code className="mono">{resetInfo.new_password}</code>
                <br /><em>Copy this password now — it won't be shown again.</em>
              </div>
            )}
            {usersLoading ? <div className="loading-state">Loading...</div> : (
              <div className="user-list">
                {users.map(user => (
                  <div key={user.id} className="user-row">
                    <div className="user-row-info">
                      <span className="user-row-name">
                        {user.username}
                        {/* Highlight the current user's own row for clarity */}
                        {user.id === currentUser.id && <span className="badge badge-sm badge-info" style={{ marginLeft: 6 }}>you</span>}
                      </span>
                      <div className="user-row-badges">
                        {user.is_admin && <span className="badge badge-sm badge-warning">admin</span>}
                        {!user.is_active && <span className="badge badge-sm badge-error">disabled</span>}
                        {user.must_change_password && <span className="badge badge-sm badge-info">pw change</span>}
                        {user.auth_provider === 'oidc' && <span className="badge badge-sm badge-oidc">SSO</span>}
                      </div>
                    </div>
                    <div className="user-row-actions">
                      <button className="btn btn-xs btn-ghost"
                        onClick={() => handleToggleAdmin(user.id, user.is_admin)}
                        title={user.is_admin ? 'Remove admin' : 'Make admin'}>
                        {user.is_admin ? '⬇ Demote' : '⬆ Promote'}
                      </button>
                      <button className="btn btn-xs btn-ghost"
                        onClick={() => handleToggleActive(user.id, user.is_active)}>
                        {user.is_active ? 'Disable' : 'Enable'}
                      </button>
                      {/* Opens the channel assignment modal for this user */}
                      <button className="btn btn-xs btn-accent"
                        onClick={() => handleSelectUserForAssign(user.id)}>
                        Channels
                      </button>
                      {/* OIDC users authenticate externally — no local password to reset */}
                      {user.auth_provider === 'local' && (
                        <button className="btn btn-xs btn-warning"
                          onClick={() => handleResetPassword(user.id)}>Reset PW</button>
                      )}
                      {/* Cannot delete yourself — prevents admin lockout */}
                      {user.id !== currentUser.id && (
                        <button className="btn btn-xs btn-delete"
                          onClick={() => handleDeleteUser(user.id)}>Delete</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/*
            Channel assignment modal — shown when an admin clicks "Channels" on a user row.
            Clicking the overlay backdrop closes the modal without saving.
          */}
          {selectedUserId && (
            <div className="modal-overlay" onClick={() => setSelectedUserId(null)}>
              {/* stopPropagation prevents clicks inside the modal from closing it */}
              <div className="modal-card" onClick={e => e.stopPropagation()}>
                <div className="modal-header">
                  <h2>Channel Assignments</h2>
                  <p className="modal-subtitle">
                    Assign streams to: {users.find(u => u.id === selectedUserId)?.username}
                  </p>
                </div>
                <div className="assign-list">
                  {streams.length === 0 ? <div className="empty-state">No streams created yet</div> : (
                    streams.map(stream => (
                      <label key={stream.id} className="assign-item">
                        <input type="checkbox"
                          checked={assignedStreamIds.includes(stream.id)}
                          onChange={() => handleToggleStreamAssign(stream.id)} />
                        <span>{stream.name}</span>
                        <span className="mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                          {stream.multicast_address}:{stream.multicast_port}
                        </span>
                      </label>
                    ))
                  )}
                </div>
                <div className="modal-actions" style={{ marginTop: 16 }}>
                  <button className="btn btn-primary" onClick={handleSaveAssignments}>Save</button>
                  <button className="btn btn-ghost" onClick={() => setSelectedUserId(null)}>Cancel</button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ═══ Server Settings Tab ═══════════════════════════════════════════════ */}
      {activeTab === 'server' && isAdmin && (
        <div className="settings-content">
          {settingsLoading ? <div className="loading-state">Loading settings...</div> : (<>
            {/* Render settings grouped by category for better organization */}
            {Object.entries(settingCategories).map(([category, keys]) => (
              <div key={category} className="settings-group">
                <h3 className="settings-group-title">{category}</h3>
                {keys.map(key => {
                  const s = settings.find(st => st.key === key);
                  if (!s) return null;
                  return (
                    <div key={key} className="setting-row">
                      <div className="setting-info">
                        {/* Display the setting key with underscores replaced by spaces for readability */}
                        <span className="setting-key">{s.key.replace(/_/g, ' ')}</span>
                        <span className="setting-desc">{s.description}</span>
                      </div>
                      {/* Render dropdown for boolean settings, password for secrets, text for everything else */}
                      {dropdownSettings[key] ? (
                        <select className="setting-input" value={editValues[key] || ''}
                          onChange={e => setEditValues({ ...editValues, [key]: e.target.value })}>
                          {dropdownSettings[key].map(opt => (
                            <option key={opt} value={opt}>{opt}</option>
                          ))}
                        </select>
                      ) : (
                        <input className="setting-input"
                          type={passwordSettings.includes(key) ? 'password' : 'text'}
                          value={editValues[key] || ''}
                          onChange={e => setEditValues({ ...editValues, [key]: e.target.value })} />
                      )}
                    </div>
                  );
                })}
              </div>
            ))}
            <div className="settings-actions">
              <button className="btn btn-primary" onClick={handleSaveSettings}>Save Settings</button>
              {saveStatus && <span className={`settings-status ${saveStatus.startsWith('Error') ? 'error-text' : 'success-text'}`}>{saveStatus}</span>}
            </div>
          </>)}
        </div>
      )}

      {/* ═══ Account Tab ═══════════════════════════════════════════════════════ */}
      {activeTab === 'account' && (
        <div className="settings-content">
          <div className="settings-group">
            <h3 className="settings-group-title">Account Info</h3>
            <div className="account-info-row">
              <span className="setting-key">Username</span><span className="mono">{currentUser?.username}</span>
            </div>
            <div className="account-info-row">
              <span className="setting-key">Role</span><span>{currentUser?.is_admin ? 'Administrator' : 'User'}</span>
            </div>
          </div>
          {/* OIDC users manage passwords through their identity provider */}
          {currentUser?.auth_provider !== 'oidc' && <div className="settings-group">
            <h3 className="settings-group-title">Change Password</h3>
            <form onSubmit={handleChangePassword}>
              {pwError && <div className="login-error">{pwError}</div>}
              {pwSuccess && <div className="settings-success">{pwSuccess}</div>}
              <div className="form-group" style={{ marginBottom: 12 }}>
                <label>Current Password</label>
                <input type="password" value={currentPw} onChange={e => setCurrentPw(e.target.value)} required />
              </div>
              <div className="form-group" style={{ marginBottom: 12 }}>
                <label>New Password</label>
                <input type="password" value={newPw} onChange={e => setNewPw(e.target.value)} placeholder="Minimum 8 characters" required />
              </div>
              <div className="form-group" style={{ marginBottom: 12 }}>
                <label>Confirm New Password</label>
                <input type="password" value={confirmPw} onChange={e => setConfirmPw(e.target.value)} required />
              </div>
              <button type="submit" className="btn btn-primary" disabled={pwLoading || !currentPw || !newPw || !confirmPw}>
                {pwLoading ? 'Changing...' : 'Change Password'}
              </button>
            </form>
          </div>}
        </div>
      )}
    </div>
  );
}
