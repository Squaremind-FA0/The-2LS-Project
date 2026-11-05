import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { installFakeWebSocket, FakeWebSocket } from '../test/fakeWebSocket.js';
import { mockFetch } from '../test/mockFetch.js';
import { renderWithAuth, USERS } from '../test/renderWithAuth.jsx';
import { OfferProvider, useOffers } from './OfferProvider.jsx';

function pendingOffer(id, secondsLeft = 300) {
  return {
    id,
    offer_id: id,
    job_id: 100 + id,
    worker_id: 11,
    status: 'pending',
    expires_at: new Date(Date.now() + secondsLeft * 1000).toISOString().replace('Z', ''),
    created_at: '2026-11-05T09:59:00',
    job: { id: 100 + id, trade: `trade-${id}`, address: `${id} Main St`, lat: 1, lng: 2, scheduled_for: '2026-11-06T08:00:00' },
    distance_km: 1.5,
  };
}

const created = (offer) => ({ type: 'offer.created', ...offer });

function Probe() {
  const { queue, jobs } = useOffers();
  return (
    <>
      <p data-testid="queue">{queue.map((offer) => offer.offer_id).join(',')}</p>
      <p data-testid="jobs">{jobs.map((job) => `${job.id}:${job.status}`).join(',')}</p>
    </>
  );
}

function renderProvider(auth = {}) {
  return renderWithAuth(
    <OfferProvider>
      <Probe />
    </OfferProvider>,
    { user: USERS.worker, auth: { token: 'tok', ...auth } },
  );
}

const queue = () => screen.getByTestId('queue').textContent;

async function connect(socket = FakeWebSocket.last) {
  await act(async () => {
    socket.serverOpen();
    socket.serverSend({ type: 'auth.ok' });
  });
}

beforeEach(() => {
  installFakeWebSocket();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('OfferProvider', () => {
  it('sends the auth frame first and never puts the token in the URL', () => {
    mockFetch({ 'GET /offers/pending': { body: [] } });
    renderProvider();
    const socket = FakeWebSocket.last;
    expect(socket.url).toMatch(/^ws:\/\/[^/]+\/api\/ws\/worker$/);
    act(() => socket.serverOpen());
    expect(socket.sent).toEqual([{ type: 'auth', token: 'tok' }]);
  });

  it('does not poll before auth.ok', async () => {
    const fetchMock = mockFetch({ 'GET /offers/pending': { body: [] } });
    renderProvider();
    act(() => FakeWebSocket.last.serverOpen());
    expect(fetchMock).not.toHaveBeenCalled();
    await act(async () => FakeWebSocket.last.serverSend({ type: 'auth.ok' }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('OfferProvider merges GET /api/offers/pending with queued WS events by offer_id without duplicates', async () => {
    const [one, two, three] = [pendingOffer(1, 100), pendingOffer(2, 200), pendingOffer(3, 300)];
    let answerPoll;
    mockFetch({
      'GET /offers/pending': () => ({ body: [one, two] }),
    });
    const realFetch = globalThis.fetch;
    vi.stubGlobal('fetch', async (...args) => {
      await new Promise((resolve) => {
        answerPoll = resolve;
      });
      return realFetch(...args);
    });
    renderProvider();

    await connect();
    // Pushed while the poll is still in flight: one overlaps it, one doesn't.
    await act(async () => {
      FakeWebSocket.last.serverSend(created(two));
      FakeWebSocket.last.serverSend(created(three));
    });
    expect(queue()).toBe('2,3');

    await act(async () => answerPoll());
    await waitFor(() => expect(queue()).toBe('1,2,3'));
  });

  it('answers a server ping', async () => {
    mockFetch({ 'GET /offers/pending': { body: [] } });
    renderProvider();
    await connect();
    act(() => FakeWebSocket.last.serverSend({ type: 'ping' }));
    expect(FakeWebSocket.last.sent.at(-1)).toEqual({ type: 'pong' });
  });

  it('drops offers on offer.expired and offer.taken', async () => {
    mockFetch({ 'GET /offers/pending': { body: [pendingOffer(1), pendingOffer(2), pendingOffer(3)] } });
    renderProvider();
    await connect();
    await waitFor(() => expect(queue()).toBe('1,2,3'));
    act(() => FakeWebSocket.last.serverSend({ type: 'offer.expired', offer_id: 1, job_id: 101 }));
    act(() => FakeWebSocket.last.serverSend({ type: 'offer.taken', offer_id: 3, job_id: 103 }));
    expect(queue()).toBe('2');
  });

  it('a slower poll cannot bring back an offer closed while it was in flight', async () => {
    const offer = pendingOffer(1);
    mockFetch({ 'GET /offers/pending': { body: [offer] } });
    const realFetch = globalThis.fetch;
    let answerPoll;
    vi.stubGlobal('fetch', async (...args) => {
      await new Promise((resolve) => {
        answerPoll = resolve;
      });
      return realFetch(...args);
    });
    renderProvider();
    await connect();
    act(() => FakeWebSocket.last.serverSend({ type: 'offer.taken', offer_id: 1, job_id: 101 }));
    await act(async () => answerPoll());
    await act(async () => {});
    expect(queue()).toBe('');
  });

  it('a reconnect poll forgets offers that closed while the socket was down', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockFetch({ 'GET /offers/pending': [{ body: [pendingOffer(1)] }, { body: [pendingOffer(2)] }] });
    renderProvider();
    await connect();
    await waitFor(() => expect(queue()).toBe('1'));

    act(() => FakeWebSocket.last.serverClose(1012));
    await act(async () => vi.advanceTimersByTime(30000));
    await connect();

    await waitFor(() => expect(queue()).toBe('2'));
  });

  it.each([4408, 1012, 1013, 1006])('reconnects with backoff and re-polls after close code %i', async (code) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = mockFetch({ 'GET /offers/pending': { body: [] } });
    renderProvider();
    await connect();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    const first = FakeWebSocket.last;
    act(() => first.serverClose(code));
    expect(FakeWebSocket.instances).toHaveLength(1);

    await act(async () => vi.advanceTimersByTime(30000));
    expect(FakeWebSocket.instances).toHaveLength(2);

    await connect();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(FakeWebSocket.last.sent[0]).toEqual({ type: 'auth', token: 'tok' });
  });

  it.each([4401, 4403])('forces logout and does not reconnect on close code %i', async (code) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockFetch({ 'GET /offers/pending': { body: [] } });
    const logout = vi.fn();
    renderProvider({ logout });
    await connect();

    act(() => FakeWebSocket.last.serverClose(code));

    expect(logout).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(60000));
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('closes the socket and stops reconnecting on unmount', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockFetch({ 'GET /offers/pending': { body: [] } });
    const { unmount } = renderProvider();
    await connect();
    const socket = FakeWebSocket.last;
    unmount();
    expect(socket.closed).toBe(true);
    await act(async () => vi.advanceTimersByTime(60000));
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('shows the active offer, and accepting records the job and closes the modal', async () => {
    mockFetch({
      'GET /offers/pending': { body: [pendingOffer(1)] },
      'POST /offers/1/accept': { body: { id: 1, status: 'accepted' } },
    });
    renderProvider();
    await connect();
    await screen.findByRole('dialog');

    await userEvent.click(screen.getByRole('button', { name: 'Accept job' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByTestId('jobs')).toHaveTextContent('101:assigned');
    expect(screen.getByRole('status')).toHaveTextContent(/accepted/i);
  });

  it('a 409 on Accept closes the modal with a "no longer available" message and records no job', async () => {
    mockFetch({
      'GET /offers/pending': { body: [pendingOffer(1)] },
      'POST /offers/1/accept': { status: 409, body: { detail: 'Job already assigned' } },
    });
    renderProvider();
    await connect();
    await screen.findByRole('dialog');

    await userEvent.click(screen.getByRole('button', { name: 'Accept job' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByRole('status')).toHaveTextContent(/no longer available/i);
    expect(screen.getByTestId('jobs')).toHaveTextContent('');
    expect(queue()).toBe('');
  });

  it('declining moves on to the next offer', async () => {
    mockFetch({
      'GET /offers/pending': { body: [pendingOffer(1, 100), pendingOffer(2, 200)] },
      'POST /offers/1/decline': { body: { id: 1, status: 'declined' } },
    });
    renderProvider();
    await connect();
    expect(await screen.findByRole('dialog')).toHaveTextContent('trade-1');
    await userEvent.click(screen.getByRole('button', { name: 'Decline job' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toHaveTextContent('trade-2'));
  });

  it('dismissing an expired offer keeps it from coming back on the next poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const offer = pendingOffer(1, 1);
    mockFetch({ 'GET /offers/pending': { body: [offer] } });
    renderProvider();
    await connect();
    await screen.findByRole('dialog');
    await act(async () => vi.advanceTimersByTime(2000));
    await act(async () => screen.getByRole('button', { name: 'Dismiss' }).click());
    expect(queue()).toBe('');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('keeps each worker\'s accepted jobs separate across sessions on a shared device', async () => {
    mockFetch({
      'GET /offers/pending': { body: [pendingOffer(1)] },
      'POST /offers/1/accept': { body: { id: 1, status: 'accepted' } },
    });
    const first = renderProvider();
    await connect();
    await screen.findByRole('dialog');
    await userEvent.click(screen.getByRole('button', { name: 'Accept job' }));
    await waitFor(() => expect(screen.getByTestId('jobs')).toHaveTextContent('101:assigned'));
    first.unmount();

    mockFetch({ 'GET /offers/pending': { body: [] } });
    const again = renderProvider();
    expect(screen.getByTestId('jobs')).toHaveTextContent('101:assigned');
    again.unmount();

    renderWithAuth(
      <OfferProvider>
        <Probe />
      </OfferProvider>,
      { user: { ...USERS.worker, id: 99 }, auth: { token: 'tok' } },
    );
    expect(screen.getByTestId('jobs')).toHaveTextContent('');
  });
});
