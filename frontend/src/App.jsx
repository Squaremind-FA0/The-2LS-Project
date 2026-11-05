import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

import { AuthProvider, useAuth } from './auth/AuthContext.jsx';
import { RoleGuard } from './auth/RoleGuard.jsx';
import { AdminLayout } from './layouts/AdminLayout.jsx';
import { WorkerLayout } from './layouts/WorkerLayout.jsx';
import { Login } from './pages/Login.jsx';
import './styles.css';

const HOME_BY_ROLE = { worker: '/worker', company_admin: '/admin', dispatcher: '/admin' };

const Loading = () => <p role="status">Loading…</p>;

function Home() {
  const { user, loading } = useAuth();
  if (loading) {
    return <Loading />;
  }
  return <Navigate to={user ? HOME_BY_ROLE[user.role] : '/login'} replace />;
}

function LoginRoute() {
  const { user, loading } = useAuth();
  if (loading) {
    return <Loading />;
  }
  return user ? <Navigate to="/" replace /> : <Login />;
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/login" element={<LoginRoute />} />
          <Route
            path="/worker"
            element={
              <RoleGuard allow={['worker']}>
                <WorkerLayout />
              </RoleGuard>
            }
          >
            <Route index element={<Navigate to="jobs" replace />} />
            {/* Until the area's default page is registered: without this, /worker/jobs falls to the top-level * and redirects forever. */}
            <Route path="*" element={null} />
          </Route>
          <Route
            path="/admin"
            element={
              <RoleGuard allow={['company_admin', 'dispatcher']}>
                <AdminLayout />
              </RoleGuard>
            }
          >
            <Route index element={<Navigate to="jobs" replace />} />
            {/* Until the area's default page is registered: without this, /admin/jobs falls to the top-level * and redirects forever. */}
            <Route path="*" element={null} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
