import { Navigate } from 'react-router-dom';

import { useAuth } from './AuthContext.jsx';

/**
 * UX only: moves a user away from a screen their role can't use. This is not
 * access control. The server (require_role / require_ws_role) is the sole
 * authority, and pages still handle the real 401/403 it returns.
 */
export function RoleGuard({ allow, children }) {
  const { user, loading } = useAuth();
  if (loading) {
    return <p role="status">Loading…</p>;
  }
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  if (!allow.includes(user.role)) {
    return <Navigate to="/" replace />;
  }
  return children;
}
