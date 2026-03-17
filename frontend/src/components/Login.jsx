/**
 * Login.jsx — Authentication screen.
 *
 * Presents a username/password form styled as a centered card.
 * On submit, calls the /auth/login endpoint to get a JWT, then immediately
 * calls /auth/me to fetch the full user profile. Both are needed because:
 *   - login() returns the JWT + must_change_password flag
 *   - getCurrentUser() returns the full user object (username, is_admin, etc.)
 *
 * The parent (App.jsx) receives both via onLoginSuccess to set up auth state
 * and potentially trigger the forced password change flow.
 */
import React, { useState } from 'react';
import { login, getCurrentUser } from '../api';

export default function Login({ onLoginSuccess }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      // First call gets the JWT and stores it in localStorage via api.js
      const loginResponse = await login(username, password);
      // Second call uses the now-stored JWT to fetch the full user profile
      const user = await getCurrentUser();
      onLoginSuccess(user, loginResponse);
    } catch (err) { setError(err.message); }
    finally { setLoading(false); }
  };

  return (
    <div className="login-wrapper">
      <div className="login-card">
        <div className="login-header">
          <div className="login-icon">▶</div>
          <h1>Multicast Streamer</h1>
          <p className="login-subtitle">MPEG-TS Multicast Playout System</p>
        </div>
        <form onSubmit={handleSubmit} className="login-form">
          {error && <div className="login-error">{error}</div>}
          <div className="form-group">
            <label htmlFor="username">Username</label>
            <input id="username" type="text" value={username}
              onChange={(e) => setUsername(e.target.value)} autoFocus required />
          </div>
          <div className="form-group">
            <label htmlFor="password">Password</label>
            <input id="password" type="password" value={password}
              onChange={(e) => setPassword(e.target.value)} required />
          </div>
          {/* Disabled when loading or when either field is empty to prevent double-submit */}
          <button type="submit" className="btn btn-primary login-btn"
            disabled={loading || !username || !password}>
            {loading ? 'Authenticating...' : 'Sign In'}
          </button>
        </form>
      </div>
    </div>
  );
}
