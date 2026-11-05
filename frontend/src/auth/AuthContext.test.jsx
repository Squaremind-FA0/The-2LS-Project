import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiRequest } from '../api/client.js';
import { mockFetch, sentBodies } from '../test/mockFetch.js';
import { AuthProvider, useAuth } from './AuthContext.jsx';

const ME = { id: 1, email: 'w@example.com', full_name: 'Wes', role: 'worker', company_id: 1 };

function Probe({ password = 'pw' }) {
  const { user, loading, login, logout } = useAuth();
  return (
    <div>
      <p>state: {loading ? 'loading' : (user?.role ?? 'anonymous')}</p>
      <button onClick={() => login('w@example.com', password).catch(() => {})}>log in</button>
      <button onClick={logout}>log out</button>
    </div>
  );
}

const renderProbe = () =>
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('AuthContext', () => {
  it('AuthContext.login stores the token and loads the current user from GET /api/me', async () => {
    const fetchMock = mockFetch({
      'POST /auth/login': { body: { access_token: 'tok', token_type: 'bearer', expires_in: 900 } },
      'GET /me': { body: ME },
    });
    renderProbe();
    expect(screen.getByText('state: anonymous')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'log in' }));

    expect(await screen.findByText('state: worker')).toBeInTheDocument();
    expect(localStorage.getItem('token')).toBe('tok');
    expect(sentBodies(fetchMock, 'POST /auth/login')).toEqual([{ email: 'w@example.com', password: 'pw' }]);
    const meCall = fetchMock.mock.calls.find(([url]) => url === '/api/me');
    expect(meCall[1].headers.Authorization).toBe('Bearer tok');
  });

  it('bootstraps the user from an existing token, reporting loading until it knows', async () => {
    localStorage.setItem('token', 'tok');
    mockFetch({ 'GET /me': { body: ME } });
    renderProbe();
    expect(screen.getByText('state: loading')).toBeInTheDocument();
    expect(await screen.findByText('state: worker')).toBeInTheDocument();
  });

  it('drops a token the server rejects during bootstrap', async () => {
    localStorage.setItem('token', 'stale');
    mockFetch({ 'GET /me': { status: 401, body: { detail: 'Not authenticated' } } });
    renderProbe();
    expect(await screen.findByText('state: anonymous')).toBeInTheDocument();
    expect(localStorage.getItem('token')).toBeNull();
  });

  it('logout clears the token and the user', async () => {
    localStorage.setItem('token', 'tok');
    mockFetch({ 'GET /me': { body: ME } });
    renderProbe();
    await screen.findByText('state: worker');
    await userEvent.click(screen.getByRole('button', { name: 'log out' }));
    expect(screen.getByText('state: anonymous')).toBeInTheDocument();
    expect(localStorage.getItem('token')).toBeNull();
  });

  it('ends the session when any later request comes back 401', async () => {
    localStorage.setItem('token', 'tok');
    mockFetch({
      'GET /me': { body: ME },
      'GET /anything': { status: 401, body: { detail: 'Not authenticated' } },
    });
    renderProbe();
    await screen.findByText('state: worker');
    await act(async () => {
      await apiRequest('/anything').catch(() => {});
    });
    await waitFor(() => expect(screen.getByText('state: anonymous')).toBeInTheDocument());
    expect(localStorage.getItem('token')).toBeNull();
  });

  it('leaves no token behind when login succeeds but GET /api/me fails', async () => {
    mockFetch({
      'POST /auth/login': { body: { access_token: 'tok', token_type: 'bearer', expires_in: 900 } },
      'GET /me': { status: 500, body: { detail: 'boom' } },
    });
    renderProbe();
    await userEvent.click(screen.getByRole('button', { name: 'log in' }));
    await waitFor(() => expect(localStorage.getItem('token')).toBeNull());
    expect(screen.getByText('state: anonymous')).toBeInTheDocument();
  });
});
