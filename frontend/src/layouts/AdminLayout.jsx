import { Outlet } from 'react-router-dom';

import { Nav } from '../components/Nav.jsx';

export function AdminLayout() {
  return (
    <>
      <header className="app-header">
        <Nav />
      </header>
      <main className="page">
        <Outlet />
      </main>
    </>
  );
}
