/**
 * App.jsx — Root application component.
 *
 * Manages the top-level authentication lifecycle:
 *   1. On mount, checks localStorage for an existing JWT and validates it against /auth/me.
 *   2. If valid, checks whether the server requires a forced password change (first login).
 *   3. Routes to Login, ChangePassword (forced), or the main Layout based on auth state.
 *
 * Also listens for a custom 'auth-expired' event (dispatched by api.js on 401 responses)
 * so that any API call that discovers an expired token can force a global sign-out without
 * prop-drilling a logout callback into every component.
 */
import React, { useState, useEffect, useCallback } from 'react';
import Login from './components/Login';
import Layout from './components/Layout';
import ChangePassword from './components/ChangePassword';
import { getStoredToken, getCurrentUser, logout } from './api';

export default function App() {
  /** Whether the user has a valid JWT session */
  const [isAuth, setIsAuth] = useState(false);
  /** Full user object from /auth/me (username, is_admin, etc.) */
  const [user, setUser] = useState(null);
  /** True while the initial token validation request is in-flight */
  const [checking, setChecking] = useState(true);
  /** True when the backend signals the user must set a new password before proceeding */
  const [mustChangePassword, setMustChangePassword] = useState(false);

  /**
   * Validates the stored JWT by calling /auth/me.
   * If the token is missing or the request fails, the user is considered signed out.
   * Wrapped in useCallback so it has a stable identity for the useEffect dependency array.
   */
  const checkAuth = useCallback(async () => {
    // No token in localStorage — skip the network request entirely
    if (!getStoredToken()) { setChecking(false); return; }
    try {
      const u = await getCurrentUser();
      setUser(u);
      setIsAuth(true);
      // The server sets must_change_password=true for accounts with default/reset passwords
      if (u.must_change_password) setMustChangePassword(true);
    } catch {
      // Token is invalid or expired — clear it and show the login screen
      logout();
      setIsAuth(false);
    }
    finally { setChecking(false); }
  }, []);

  useEffect(() => {
    checkAuth();
    // Listen for the global 'auth-expired' event that api.js fires on any 401 response.
    // This ensures we return to the login screen even if the 401 comes from a background
    // polling request deep inside a child component.
    const onExpired = () => { setIsAuth(false); setUser(null); };
    window.addEventListener('auth-expired', onExpired);
    return () => window.removeEventListener('auth-expired', onExpired);
  }, [checkAuth]);

  /**
   * Called by the Login component after a successful username/password authentication.
   * The login response includes must_change_password to trigger the forced change flow.
   */
  const handleLoginSuccess = (user, loginResponse) => {
    setUser(user);
    setIsAuth(true);
    if (loginResponse.must_change_password) setMustChangePassword(true);
  };

  /**
   * Called after the forced password change succeeds.
   * Re-fetches the user object to clear the must_change_password flag on the server side.
   */
  const handlePasswordChanged = async () => {
    setMustChangePassword(false);
    try { const u = await getCurrentUser(); setUser(u); } catch {}
  };

  /** Full sign-out — clears the JWT and resets all auth state. */
  const handleLogout = () => {
    logout(); setIsAuth(false); setUser(null); setMustChangePassword(false);
  };

  // Show a spinner while the initial token validation is in progress
  if (checking) return <div className="app-loading"><div className="loading-spinner" /></div>;
  // Render the auth gate: Login → forced ChangePassword → main Layout
  if (!isAuth) return <Login onLoginSuccess={handleLoginSuccess} />;
  if (mustChangePassword) return <ChangePassword isForced={true} onComplete={handlePasswordChanged} />;

  return <Layout currentUser={user} onLogout={handleLogout} />;
}
