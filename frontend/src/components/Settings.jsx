/**
 * Settings panel — Server Settings (admin), User Management (admin), Account Settings (all).
 */
import React, { useState, useEffect } from 'react';
import {
  getSettings, updateSettings, changePassword,
  listUsers, createUser, updateUser, resetUserPassword, deleteUser,
  listStreams, assignStreamUsers,
} from '../api';

export default function Settings({ currentUser }) {
  const isAdmin = currentUser?.is_admin;
  const [activeTab, setActiveTab] = useState(isAdmin ? 'users' : 'account');

  // --- Server settings state ---
  const [settings, setSettings] = useState([]);
  const [editValues, setEditValues] = useState({});
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [saveStatus, setSaveStatus] = useState('');

  // --- User management state ---
  const [users, setUsers] = useState([]);
  const [usersLoading, setUsersLoading] = useState(true);
  const [newUsername, setNewUsername] = useState('');
  const [newIsAdmin, setNewIsAdmin] = useState(false);
  const [createdUserInfo, setCreatedUserInfo] = useState(null);
  const [resetInfo, setResetInfo] = useState(null);
  const [userError, setUserError] = useState('');

  // --- Stream assignment state ---
  const [streams, setStreams] = useState([]);
  const [selectedUserId, setSelectedUserId] = useState(null);
  const [assignedStreamIds, setAssignedStreamIds] = useState([]);

  // --- Password state ---
  const [currentPw, setCurrentPw] = useState('');
  const [newPw, setNewPw] = useState('');
  const [confirmPw, setConfirmPw] = useState('');
  const [pwError, setPwError] = useState('');
  const [pwSuccess, setPwSuccess] = useState('');
  const [pwLoading, setPwLoading] = useState(false);

  useEffect(() => { if (isAdmin) { loadSettings(); loadUsers(); loadStreams(); } }, [isAdmin]);

  const loadSettings = async () => {
    try {
      const data = await getSettings();
      setSettings(data);
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

  const loadStreams = async () => {
    try { const d = await listStreams(); setStreams(d.streams); }
    catch (err) { console.error(err); }
  };

  const handleSaveSettings = async () => {
    setSaveStatus('');
    const changed = {};
    settings.forEach(s => { if (editValues[s.key] !== s.value) changed[s.key] = editValues[s.key]; });
    if (Object.keys(changed).length === 0) { setSaveStatus('No changes'); return; }
    try { await updateSettings(changed); setSaveStatus(`Saved ${Object.keys(changed).length} setting(s)`); loadSettings(); }
    catch (err) { setSaveStatus(`Error: ${err.message}`); }
  };

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

  const handleToggleAdmin = async (userId, currentIsAdmin) => {
    try { await updateUser(userId, { is_admin: !currentIsAdmin }); loadUsers(); }
    catch (err) { setUserError(err.message); }
  };

  const handleToggleActive = async (userId, currentIsActive) => {
    try { await updateUser(userId, { is_active: !currentIsActive }); loadUsers(); }
    catch (err) { setUserError(err.message); }
  };

  const handleResetPassword = async (userId) => {
    setResetInfo(null);
    try { const r = await resetUserPassword(userId); setResetInfo({ userId, ...r }); loadUsers(); }
    catch (err) { setUserError(err.message); }
  };

  const handleDeleteUser = async (userId) => {
    if (!window.confirm('Delete this user? Their assets will remain but become unowned.')) return;
    try { await deleteUser(userId); loadUsers(); }
    catch (err) { setUserError(err.message); }
  };

  const handleSelectUserForAssign = (userId) => {
    setSelectedUserId(userId);
    const user = users.find(u => u.id === userId);
    setAssignedStreamIds(user?.assigned_stream_ids || []);
  };

  const handleToggleStreamAssign = (streamId) => {
    setAssignedStreamIds(prev =>
      prev.includes(streamId) ? prev.filter(id => id !== streamId) : [...prev, streamId]
    );
  };

  const handleSaveAssignments = async () => {
    // Update each stream's user list to include/exclude selectedUserId
    for (const stream of streams) {
      const currentAssigned = stream.assigned_user_ids || [];
      const shouldBeAssigned = assignedStreamIds.includes(stream.id);
      const isCurrentlyAssigned = currentAssigned.includes(selectedUserId);

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
    loadUsers(); loadStreams();
    setSelectedUserId(null);
  };

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

  const settingCategories = {
    'Stream Limits': ['max_concurrent_streams', 'max_cpu_utilization', 'max_bandwidth_utilization'],
    'Transcode Profile': ['transcode_resolution', 'transcode_framerate', 'transcode_video_bitrate',
                          'transcode_audio_bitrate', 'transcode_video_preset', 'transcode_video_profile'],
    'Media Defaults': ['static_image_duration'],
    'Multicast Defaults': ['default_multicast_address', 'default_multicast_port', 'multicast_ttl'],
  };

  return (
    <div className="settings-panel">
      <div className="settings-tabs">
        {isAdmin && <button className={`settings-tab ${activeTab === 'users' ? 'active' : ''}`}
          onClick={() => setActiveTab('users')}>User Management</button>}
        {isAdmin && <button className={`settings-tab ${activeTab === 'server' ? 'active' : ''}`}
          onClick={() => setActiveTab('server')}>Server Settings</button>}
        <button className={`settings-tab ${activeTab === 'account' ? 'active' : ''}`}
          onClick={() => setActiveTab('account')}>Account</button>
      </div>

      {/* === User Management === */}
      {activeTab === 'users' && isAdmin && (
        <div className="settings-content">
          {/* Create user */}
          <div className="settings-group">
            <h3 className="settings-group-title">Create User</h3>
            {userError && <div className="login-error">{userError}</div>}
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

          {/* User list */}
          <div className="settings-group">
            <h3 className="settings-group-title">Users ({users.length})</h3>
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
                        {user.id === currentUser.id && <span className="badge badge-sm badge-info" style={{ marginLeft: 6 }}>you</span>}
                      </span>
                      <div className="user-row-badges">
                        {user.is_admin && <span className="badge badge-sm badge-warning">admin</span>}
                        {!user.is_active && <span className="badge badge-sm badge-error">disabled</span>}
                        {user.must_change_password && <span className="badge badge-sm badge-info">pw change</span>}
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
                      <button className="btn btn-xs btn-accent"
                        onClick={() => handleSelectUserForAssign(user.id)}>
                        Channels
                      </button>
                      <button className="btn btn-xs btn-warning"
                        onClick={() => handleResetPassword(user.id)}>Reset PW</button>
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

          {/* Channel assignment modal */}
          {selectedUserId && (
            <div className="modal-overlay" onClick={() => setSelectedUserId(null)}>
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

      {/* === Server Settings === */}
      {activeTab === 'server' && isAdmin && (
        <div className="settings-content">
          {settingsLoading ? <div className="loading-state">Loading settings...</div> : (<>
            {Object.entries(settingCategories).map(([category, keys]) => (
              <div key={category} className="settings-group">
                <h3 className="settings-group-title">{category}</h3>
                {keys.map(key => {
                  const s = settings.find(st => st.key === key);
                  if (!s) return null;
                  return (
                    <div key={key} className="setting-row">
                      <div className="setting-info">
                        <span className="setting-key">{s.key.replace(/_/g, ' ')}</span>
                        <span className="setting-desc">{s.description}</span>
                      </div>
                      <input className="setting-input" type="text" value={editValues[key] || ''}
                        onChange={e => setEditValues({ ...editValues, [key]: e.target.value })} />
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

      {/* === Account === */}
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
          <div className="settings-group">
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
          </div>
        </div>
      )}
    </div>
  );
}
