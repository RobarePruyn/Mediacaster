/**
 * Login.jsx — Authentication screen with local login and optional OIDC SSO.
 *
 * Presents a username/password form styled as a centered card.
 * On submit, calls the /auth/login endpoint to get a JWT, then immediately
 * calls /auth/me to fetch the full user profile. Both are needed because:
 *   - login() returns the JWT + must_change_password flag
 *   - getCurrentUser() returns the full user object (username, is_admin, etc.)
 *
 * When OIDC is enabled (checked on mount via getOIDCConfig), a "Sign in with {name}"
 * button appears below a divider. Clicking it redirects the browser to the IdP.
 *
 * The parent (App.jsx) receives both via onLoginSuccess to set up auth state
 * and potentially trigger the forced password change flow.
 */
import React, { useState, useEffect } from 'react';
import { login, getCurrentUser, getOIDCConfig, getOIDCAuthorizeUrl } from '../api';

export default function Login({ onLoginSuccess, ssoError }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(ssoError || '');
  const [loading, setLoading] = useState(false);

  // OIDC SSO state — fetched on mount to decide whether to show the SSO button
  const [oidcEnabled, setOidcEnabled] = useState(false);
  const [oidcDisplayName, setOidcDisplayName] = useState('SSO');
  const [ssoLoading, setSsoLoading] = useState(false);

  // Update error if ssoError prop changes (e.g. from callback failure)
  useEffect(() => { if (ssoError) setError(ssoError); }, [ssoError]);

  // Check if OIDC is enabled on mount
  useEffect(() => {
    getOIDCConfig()
      .then(cfg => {
        setOidcEnabled(cfg.enabled);
        setOidcDisplayName(cfg.display_name || 'SSO');
      })
      .catch(() => {
        // OIDC not available — silently hide the button
      });
  }, []);

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

  /** Initiates the OIDC flow by redirecting to the identity provider. */
  const handleSSOLogin = async () => {
    setError('');
    setSsoLoading(true);
    try {
      // The callback URL is the app's root with /auth/callback path
      const redirectUri = `${window.location.origin}/auth/callback`;
      const data = await getOIDCAuthorizeUrl(redirectUri);
      // Redirect the browser to the IdP's authorization page
      window.location.href = data.authorization_url;
    } catch (err) {
      setError(err.message);
      setSsoLoading(false);
    }
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

        {/* SSO section — only visible when OIDC is enabled in server settings */}
        {oidcEnabled && (
          <>
            <div className="login-divider"><span>or</span></div>
            <button className="btn btn-sso" onClick={handleSSOLogin}
              disabled={ssoLoading}>
              {ssoLoading ? 'Redirecting...' : `Sign in with ${oidcDisplayName}`}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
