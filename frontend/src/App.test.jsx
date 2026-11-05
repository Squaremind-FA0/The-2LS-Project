import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from './App.jsx';
import { installFakeWebSocket } from './test/fakeWebSocket.js';
import { mockFetch } from './test/mockFetch.js';
import { USERS } from './test/renderWithAuth.jsx';

function renderAt(path, user = null) {
  window.history.pushState({}, '', path);
  if (user) {
    localStorage.setItem('token', 'tok');
  }
  mockFetch({
    'GET /me': { body: user ?? {} },
    'GET /jobs': { body: [] },
    'GET /offers/pending': { body: [] },
    'GET /shifts': { body: [] },
    'GET /payruns/7/paylines': { body: [] },
  });
  return render(<App />);
}

const navHrefs = async () => {
  const nav = await screen.findByRole('navigation', { name: 'Main' });
  return [...nav.querySelectorAll('a')].map((link) => link.getAttribute('href'));
};

beforeEach(() => {
  installFakeWebSocket();
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('App', () => {
  it('App renders the real Login form at / when unauthenticated, and redirects an authenticated user away from the wrong area', async () => {
    const anonymous = renderAt('/');
    expect(await screen.findByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/login');
    anonymous.unmount();
    localStorage.clear();

    const worker = renderAt('/admin', USERS.worker);
    expect(await navHrefs()).toContain('/worker/jobs');
    await waitFor(() => expect(window.location.pathname).toBe('/worker/jobs'));
    worker.unmount();
    localStorage.clear();

    renderAt('/worker', USERS.dispatcher);
    expect(await navHrefs()).toContain('/admin/jobs');
    await waitFor(() => expect(window.location.pathname).toBe('/admin/jobs'));
  });

  it('redirects an unknown path through / to the login form when unauthenticated', async () => {
    renderAt('/no/such/page');
    expect(await screen.findByLabelText('Email')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/login');
  });

  it('sends an already authenticated user from /login to their own area', async () => {
    renderAt('/login', USERS.company_admin);
    expect(await navHrefs()).toContain('/admin/payruns');
    await waitFor(() => expect(window.location.pathname).toBe('/admin/jobs'));
  });

  it('sends an unknown path inside an area back to that area\'s default page', async () => {
    renderAt('/worker/no-such-page', USERS.worker);
    await navHrefs();
    await waitFor(() => expect(window.location.pathname).toBe('/worker/jobs'));
  });

  it('routes every worker page', async () => {
    renderAt('/worker/pay/7', USERS.worker);
    expect(await screen.findByRole('heading', { name: 'Pay run 7' })).toBeInTheDocument();
  });

  it('does not flash /login while restoring a session on reload', async () => {
    renderAt('/worker/shifts', USERS.worker);
    expect(window.location.pathname).toBe('/worker/shifts');
    await navHrefs();
    expect(window.location.pathname).toBe('/worker/shifts');
  });
});
