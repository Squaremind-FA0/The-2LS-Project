import { screen } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { renderWithAuth, USERS } from '../test/renderWithAuth.jsx';
import { RoleGuard } from './RoleGuard.jsx';

const tree = (
  <Routes>
    <Route path="/" element={<p>home</p>} />
    <Route path="/login" element={<p>login page</p>} />
    <Route
      path="/admin"
      element={
        <RoleGuard allow={['company_admin', 'dispatcher']}>
          <p>secret admin</p>
        </RoleGuard>
      }
    />
  </Routes>
);

describe('RoleGuard', () => {
  it('RoleGuard redirects a user whose role is not in allow, and an unauthenticated user to /login', () => {
    const wrongRole = renderWithAuth(tree, { user: USERS.worker, route: '/admin' });
    expect(screen.getByText('home')).toBeInTheDocument();
    expect(screen.queryByText('secret admin')).not.toBeInTheDocument();
    wrongRole.unmount();

    renderWithAuth(tree, { user: null, route: '/admin' });
    expect(screen.getByText('login page')).toBeInTheDocument();
    expect(screen.queryByText('secret admin')).not.toBeInTheDocument();
  });

  it('renders the children for an allowed role', () => {
    renderWithAuth(tree, { user: USERS.dispatcher, route: '/admin' });
    expect(screen.getByText('secret admin')).toBeInTheDocument();
  });

  it('neither redirects nor renders while the session is still being restored', () => {
    renderWithAuth(tree, { user: null, route: '/admin', auth: { loading: true } });
    expect(screen.queryByText('login page')).not.toBeInTheDocument();
    expect(screen.queryByText('secret admin')).not.toBeInTheDocument();
  });
});
