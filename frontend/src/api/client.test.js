import { afterEach, describe, expect, it, vi } from 'vitest';

import { mockFetch, sentBodies } from '../test/mockFetch.js';
import { ApiError, apiRequest, auth, jobs, offers, payruns, UNAUTHORIZED_EVENT } from './client.js';

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('apiRequest', () => {
  it('apiRequest throws ApiError with status and parsed detail on a non-2xx response', async () => {
    mockFetch({ 'GET /thing': { status: 409, body: { detail: 'Job already assigned' } } });
    const error = await apiRequest('/thing').catch((err) => err);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.detail).toBe('Job already assigned');
    expect(error.message).toBe('Job already assigned');
  });

  it('flattens a FastAPI 422 validation list into a readable string detail', async () => {
    mockFetch({
      'GET /thing': {
        status: 422,
        body: {
          detail: [
            { loc: ['body', 'initial_password'], msg: 'String should have at least 12 characters' },
          ],
        },
      },
    });
    const error = await apiRequest('/thing').catch((err) => err);
    expect(error.detail).toBe('initial_password: String should have at least 12 characters');
  });

  it('falls back to a generic detail when the error body is not JSON', async () => {
    mockFetch({ 'GET /thing': { status: 502, body: '<html>Bad gateway</html>' } });
    const error = await apiRequest('/thing').catch((err) => err);
    expect(error.status).toBe(502);
    expect(error.detail).toBe('Request failed (502)');
  });

  it('sends JSON bodies with a content type and the bearer token', async () => {
    localStorage.setItem('token', 'abc');
    const fetchMock = mockFetch({ 'POST /thing': { status: 201, body: { id: 1 } } });
    await expect(apiRequest('/thing', { method: 'POST', json: { a: 1 } })).resolves.toEqual({ id: 1 });
    const [, options] = fetchMock.mock.calls[0];
    expect(options.headers.Authorization).toBe('Bearer abc');
    expect(options.headers['Content-Type']).toBe('application/json');
    expect(sentBodies(fetchMock, 'POST /thing')).toEqual([{ a: 1 }]);
  });

  it('announces a 401 on an authenticated request so the session can end', async () => {
    localStorage.setItem('token', 'stale');
    mockFetch({ 'GET /thing': { status: 401, body: { detail: 'Not authenticated' } } });
    const listener = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, listener);
    await apiRequest('/thing').catch(() => {});
    window.removeEventListener(UNAUTHORIZED_EVENT, listener);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('does not treat a failed login as an expired session', async () => {
    localStorage.setItem('token', 'stale');
    mockFetch({ 'POST /auth/login': { status: 401, body: { detail: 'Invalid credentials' } } });
    const listener = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, listener);
    const error = await auth.login('a@b.co', 'wrong').catch((err) => err);
    window.removeEventListener(UNAUTHORIZED_EVENT, listener);
    expect(error.detail).toBe('Invalid credentials');
    expect(listener).not.toHaveBeenCalled();
  });
});

describe('resource functions', () => {
  it('address the documented endpoints', async () => {
    const fetchMock = mockFetch({
      'POST /offers/7/accept': { body: { id: 7 } },
      'POST /jobs/3/clock-in': { body: { id: 1 } },
      'POST /jobs/3/clock-out': { body: { id: 1 } },
      'PATCH /payruns/2': { body: { id: 2 } },
      'GET /jobs?status=draft&limit=5': { body: [] },
    });
    await offers.accept(7);
    await jobs.clockIn(3);
    await jobs.clockOut(3);
    await payruns.transition(2, 'approved');
    await jobs.list({ status: 'draft', limit: 5 });
    expect(sentBodies(fetchMock, 'PATCH /payruns/2')).toEqual([{ status: 'approved' }]);
    expect(payruns.exportUrl(2)).toBe('/api/payruns/2/export?format=csv');
  });
});
