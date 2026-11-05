import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { OfferProvider } from '../../offers/OfferProvider.jsx';
import { FakeWebSocket, installFakeWebSocket } from '../../test/fakeWebSocket.js';
import { mockFetch } from '../../test/mockFetch.js';
import { renderWithAuth, USERS } from '../../test/renderWithAuth.jsx';
import { WorkerJobsPage } from './WorkerJobsPage.jsx';

const offer = () => ({
  id: 1,
  offer_id: 1,
  job_id: 9,
  worker_id: 11,
  status: 'pending',
  expires_at: new Date(Date.now() + 300000).toISOString().replace('Z', ''),
  created_at: '2026-11-05T09:59:00',
  job: { id: 9, trade: 'plumbing', address: '1 Pipe St', lat: 1, lng: 2, scheduled_for: '2026-11-06T08:00:00' },
  distance_km: 1.5,
});

function renderPage() {
  return renderWithAuth(
    <Routes>
      <Route
        path="/worker/jobs"
        element={
          <OfferProvider>
            <WorkerJobsPage />
          </OfferProvider>
        }
      />
    </Routes>,
    { user: USERS.worker, route: '/worker/jobs' },
  );
}

async function acceptOffer() {
  await act(async () => {
    FakeWebSocket.last.serverOpen();
    FakeWebSocket.last.serverSend({ type: 'auth.ok' });
  });
  await screen.findByRole('dialog');
  await userEvent.click(screen.getByRole('button', { name: 'Accept job' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  return screen.findByRole('listitem');
}

beforeEach(() => {
  installFakeWebSocket();
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('WorkerJobsPage', () => {
  it('names the backend gap in its empty state', () => {
    mockFetch({ 'GET /offers/pending': { body: [] } });
    renderPage();
    expect(screen.getByText(/only.*accepted on this device/i)).toBeInTheDocument();
  });

  it('WorkerJobsPage Start/Complete call clock-in/clock-out and move the job through in_progress to completed', async () => {
    const fetchMock = mockFetch({
      'GET /offers/pending': { body: [offer()] },
      'POST /offers/1/accept': { body: { id: 1, status: 'accepted' } },
      'POST /jobs/9/clock-in': { body: { id: 1, job_id: 9, worker_id: 11, started_at: '2026-11-05T10:00:00', ended_at: null } },
      'POST /jobs/9/clock-out': { body: { id: 1, job_id: 9, worker_id: 11, started_at: '2026-11-05T10:00:00', ended_at: '2026-11-05T11:00:00' } },
    });
    renderPage();

    const card = within(await acceptOffer());
    expect(card.getByText('plumbing')).toBeInTheDocument();
    expect(card.getByText('1 Pipe St')).toBeInTheDocument();
    expect(card.getByText('assigned')).toBeInTheDocument();
    expect(card.queryByRole('button', { name: 'Complete' })).not.toBeInTheDocument();

    await userEvent.click(card.getByRole('button', { name: 'Start' }));
    expect(await card.findByText('in_progress')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url, o]) => url === '/api/jobs/9/clock-in' && o.method === 'POST')).toBe(true);
    expect(card.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument();

    await userEvent.click(card.getByRole('button', { name: 'Complete' }));
    expect(await card.findByText('completed')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url, o]) => url === '/api/jobs/9/clock-out' && o.method === 'POST')).toBe(true);
    expect(card.queryByRole('button')).not.toBeInTheDocument();
  });

  it('surfaces a clock-in refusal and leaves the job where it was', async () => {
    mockFetch({
      'GET /offers/pending': { body: [offer()] },
      'POST /offers/1/accept': { body: { id: 1, status: 'accepted' } },
      'POST /jobs/9/clock-in': { status: 409, body: { detail: 'Worker is not on shift' } },
    });
    renderPage();
    const card = within(await acceptOffer());
    await userEvent.click(card.getByRole('button', { name: 'Start' }));
    expect(await card.findByRole('alert')).toHaveTextContent('Worker is not on shift');
    expect(card.getByText('assigned')).toBeInTheDocument();
  });
});
