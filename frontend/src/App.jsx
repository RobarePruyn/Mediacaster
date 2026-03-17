/**
 * Root component — auth state, forced password change, then Layout.
 */
import React, { useState, useEffect, useCallback } from 'react';
import Login from './components/Login';
import Layout from './components/Layout';
import ChangePassword from './components/ChangePassword';
import { getStoredToken, getCurrentUser, logout } from './api';

export default function App() {
  const [isAuth, setIsAuth] = useState(false);
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(true);
  const [mustChangePassword, setMustChangePassword] = useState(false);

  const checkAuth = useCallback(async () => {
    if (!getStoredToken()) { setChecking(false); return; }
    try {
      const u = await getCurrentUser();
      setUser(u);
      setIsAuth(true);
      if (u.must_change_password) setMustChangePassword(true);
    } catch { logout(); setIsAuth(false); }
    finally { setChecking(false); }
  }, []);

  useEffect(() => {
    checkAuth();
    const onExpired = () => { setIsAuth(false); setUser(null); };
    window.addEventListener('auth-expired', onExpired);
    return () => window.removeEventListener('auth-expired', onExpired);
  }, [checkAuth]);

  const handleLoginSuccess = (user, loginResponse) => {
    setUser(user);
    setIsAuth(true);
    if (loginResponse.must_change_password) setMustChangePassword(true);
  };

  const handlePasswordChanged = async () => {
    setMustChangePassword(false);
    try { const u = await getCurrentUser(); setUser(u); } catch {}
  };

  const handleLogout = () => {
    logout(); setIsAuth(false); setUser(null); setMustChangePassword(false);
  };

  if (checking) return <div className="app-loading"><div className="loading-spinner" /></div>;
  if (!isAuth) return <Login onLoginSuccess={handleLoginSuccess} />;
  if (mustChangePassword) return <ChangePassword isForced={true} onComplete={handlePasswordChanged} />;

  return <Layout currentUser={user} onLogout={handleLogout} />;
}
