import { screen } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FakeWebSocket, installFakeWebSocket } from '../test/fakeWebSocket.js';
import { renderWithAuth, USERS } from '../test/renderWithAuth.jsx';
import { WorkerLayout } from './WorkerLayout.jsx';

beforeEach(() => {
  installFakeWebSocket();
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('WorkerLayout', () => {
  it('WorkerLayout renders Nav, ShareLocationToggle and the active child route', () => {
    renderWithAuth(
      <Routes>
        <Route path="/worker" element={<WorkerLayout />}>
          <Route path="jobs" element={<p>child route</p>} />
        </Route>
      </Routes>,
      { user: USERS.worker, route: '/worker/jobs' },
    );
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Share my location' })).toBeInTheDocument();
    expect(screen.getByText('child route')).toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
