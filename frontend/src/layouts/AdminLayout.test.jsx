import { screen } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { renderWithAuth, USERS } from '../test/renderWithAuth.jsx';
import { AdminLayout } from './AdminLayout.jsx';

describe('AdminLayout', () => {
  it('AdminLayout renders Nav and the active child route', () => {
    renderWithAuth(
      <Routes>
        <Route path="/admin" element={<AdminLayout />}>
          <Route path="jobs" element={<p>child route</p>} />
        </Route>
      </Routes>,
      { user: USERS.dispatcher, route: '/admin/jobs' },
    );
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument();
    expect(screen.getByText('child route')).toBeInTheDocument();
  });
});
