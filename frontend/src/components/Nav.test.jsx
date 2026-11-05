import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { renderWithAuth, USERS } from '../test/renderWithAuth.jsx';
import { Nav } from './Nav.jsx';

const hrefs = () => screen.getAllByRole('link').map((link) => link.getAttribute('href'));

describe('Nav', () => {
  it('worker role cannot see /admin nav', () => {
    renderWithAuth(<Nav />, { user: USERS.worker });
    expect(hrefs()).toEqual(['/worker/jobs', '/worker/shifts', '/worker/pay']);
    expect(hrefs().some((href) => href.startsWith('/admin'))).toBe(false);
  });

  it('company_admin sees Workers, Jobs, Roster and PayRuns', () => {
    renderWithAuth(<Nav />, { user: USERS.company_admin });
    expect(hrefs()).toEqual(['/admin/workers', '/admin/jobs', '/admin/roster', '/admin/payruns']);
  });

  it('dispatcher has no PayRuns link', () => {
    renderWithAuth(<Nav />, { user: USERS.dispatcher });
    expect(hrefs()).toEqual(['/admin/workers', '/admin/jobs', '/admin/roster']);
  });

  it('offers a way to log out', async () => {
    const logout = vi.fn();
    renderWithAuth(<Nav />, { user: USERS.worker, auth: { logout } });
    await userEvent.click(screen.getByRole('button', { name: 'Log out' }));
    expect(logout).toHaveBeenCalledTimes(1);
  });
});
