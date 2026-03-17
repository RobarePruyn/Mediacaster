/**
 * ChangePassword.jsx — Password change modal/overlay.
 *
 * Used in two contexts:
 *   1. Forced mode (isForced=true): Shown as a blocking overlay after first login when the
 *      backend sets must_change_password=true. The user cannot dismiss it — they must set a
 *      new password before accessing the application. No cancel button is rendered.
 *   2. Voluntary mode (isForced=false): Triggered from the Settings > Account panel. The user
 *      can cancel and return to settings without changing their password.
 *
 * Includes client-side validation (minimum length, confirmation match) before hitting
 * the server, to avoid unnecessary round-trips for obvious input errors.
 */
import React, { useState } from 'react';
import { changePassword } from '../api';

export default function ChangePassword({ isForced, onComplete, onCancel }) {
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [errorMessage, setErrorMessage] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setErrorMessage('');

    // Client-side validation — catches common mistakes before making a server request
    if (newPassword.length < 8) {
      setErrorMessage('New password must be at least 8 characters');
      return;
    }
    if (newPassword !== confirmPassword) {
      setErrorMessage('New passwords do not match');
      return;
    }

    setIsSubmitting(true);
    try {
      // Server validates current_password and enforces any additional password policies
      await changePassword(currentPassword, newPassword);
      onComplete();
    } catch (err) {
      setErrorMessage(err.message);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay">
      <div className="modal-card">
        <div className="modal-header">
          <h2>{isForced ? 'Change Default Password' : 'Change Password'}</h2>
          {/* Explain why this is being shown when it's a forced change */}
          {isForced && (
            <p className="modal-subtitle">
              You must change the default password before continuing.
            </p>
          )}
        </div>

        <form onSubmit={handleSubmit} className="modal-form">
          {errorMessage && <div className="login-error">{errorMessage}</div>}

          <div className="form-group">
            <label htmlFor="current-pw">Current Password</label>
            <input
              id="current-pw"
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              autoFocus
              required
            />
          </div>

          <div className="form-group">
            <label htmlFor="new-pw">New Password</label>
            <input
              id="new-pw"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="Minimum 8 characters"
              required
            />
          </div>

          <div className="form-group">
            <label htmlFor="confirm-pw">Confirm New Password</label>
            <input
              id="confirm-pw"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
            />
          </div>

          <div className="modal-actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={isSubmitting || !currentPassword || !newPassword || !confirmPassword}
            >
              {isSubmitting ? 'Changing...' : 'Change Password'}
            </button>
            {/* Cancel button is only shown for voluntary password changes, not forced ones */}
            {!isForced && (
              <button type="button" className="btn btn-ghost" onClick={onCancel}>
                Cancel
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
