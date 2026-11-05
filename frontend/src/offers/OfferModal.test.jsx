import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { mockFetch } from '../test/mockFetch.js';
import { OfferModal } from './OfferModal.jsx';

const NOW = new Date('2026-11-05T10:00:00Z');

function offerExpiringIn(seconds) {
  const expiresAt = new Date(Date.now() + seconds * 1000).toISOString().replace('Z', '');
  return {
    id: 5,
    offer_id: 5,
    job_id: 9,
    worker_id: 11,
    status: 'pending',
    expires_at: expiresAt,
    created_at: '2026-11-05T09:59:00',
    job: { id: 9, trade: 'plumbing', address: '1 Pipe St', lat: 1, lng: 2, scheduled_for: '2026-11-06T08:00:00' },
    distance_km: 4.25,
  };
}

const accept = () => screen.getByRole('button', { name: /accept/i });
const decline = () => screen.getByRole('button', { name: /decline/i });

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('OfferModal', () => {
  it('offer modal renders and countdown hits 0 → disabled', () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    render(<OfferModal offer={offerExpiringIn(2)} />);

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveTextContent('plumbing');
    expect(dialog).toHaveTextContent('1 Pipe St');
    expect(dialog).toHaveTextContent('4.3 km');
    expect(screen.getByRole('timer')).toHaveTextContent('2');
    expect(accept()).toBeEnabled();
    expect(decline()).toBeEnabled();

    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByRole('timer')).toHaveTextContent('1');
    expect(accept()).toBeEnabled();

    act(() => vi.advanceTimersByTime(1000));
    expect(accept()).toBeDisabled();
    expect(decline()).toBeDisabled();
  });

  it('reads the naive expires_at as UTC, whatever the browser timezone', () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    render(<OfferModal offer={{ ...offerExpiringIn(0), expires_at: '2026-11-05T10:00:30' }} />);
    expect(screen.getByRole('timer')).toHaveTextContent('30');
  });

  it('copes with an unknown distance', () => {
    render(<OfferModal offer={{ ...offerExpiringIn(30), distance_km: null }} />);
    expect(screen.getByRole('dialog')).toHaveTextContent('Distance unknown');
  });

  it('an expired modal can be dismissed, so it never traps the worker', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const onDismiss = vi.fn();
    const offer = offerExpiringIn(1);
    render(<OfferModal offer={offer} onDismiss={onDismiss} />);
    expect(screen.queryByRole('button', { name: 'Dismiss' })).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1000));

    const dismiss = screen.getByRole('button', { name: 'Dismiss' });
    expect(dismiss).toHaveFocus();
    act(() => dismiss.click());
    expect(onDismiss).toHaveBeenCalledWith(offer);
  });

  it('modal is keyboard-focus-trapped with labelled buttons', async () => {
    const user = userEvent.setup();
    render(
      <>
        <button>behind the modal</button>
        <OfferModal offer={offerExpiringIn(30)} />
      </>,
    );
    expect(screen.getByRole('dialog', { name: /job offer/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Accept job' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Decline job' })).toBeInTheDocument();

    expect(accept()).toHaveFocus();
    await user.tab();
    expect(decline()).toHaveFocus();
    await user.tab();
    expect(accept()).toHaveFocus();
    await user.tab({ shift: true });
    expect(decline()).toHaveFocus();
    expect(screen.getByRole('button', { name: 'behind the modal' })).not.toHaveFocus();
  });

  it('Accept calls POST /api/offers/{id}/accept and handles 409 "taken"', async () => {
    const fetchMock = mockFetch({
      'POST /offers/5/accept': { status: 409, body: { detail: 'Job already assigned' } },
    });
    const onUnavailable = vi.fn();
    const offer = offerExpiringIn(30);
    render(<OfferModal offer={offer} onUnavailable={onUnavailable} />);

    await userEvent.click(accept());

    await waitFor(() => expect(onUnavailable).toHaveBeenCalledTimes(1));
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/offers/5/accept');
    expect(options.method).toBe('POST');
    expect(onUnavailable.mock.calls[0][0]).toBe(offer);
    expect(onUnavailable.mock.calls[0][1]).toMatch(/no longer available/i);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('handles 410 "Offer expired" the same way', async () => {
    mockFetch({ 'POST /offers/5/accept': { status: 410, body: { detail: 'Offer expired' } } });
    const onUnavailable = vi.fn();
    render(<OfferModal offer={offerExpiringIn(30)} onUnavailable={onUnavailable} />);
    await userEvent.click(accept());
    await waitFor(() => expect(onUnavailable).toHaveBeenCalledTimes(1));
    expect(onUnavailable.mock.calls[0][1]).toMatch(/no longer available/i);
  });

  it('stays open with the error and retryable buttons on an unexpected failure', async () => {
    mockFetch({ 'POST /offers/5/accept': { status: 500, body: { detail: 'Boom' } } });
    const onUnavailable = vi.fn();
    render(<OfferModal offer={offerExpiringIn(30)} onUnavailable={onUnavailable} />);
    await userEvent.click(accept());
    expect(await screen.findByRole('alert')).toHaveTextContent('Boom');
    expect(onUnavailable).not.toHaveBeenCalled();
    expect(accept()).toBeEnabled();
  });

  it('OfferModal Decline calls POST /api/offers/{id}/decline', async () => {
    const fetchMock = mockFetch({ 'POST /offers/5/decline': { body: { id: 5, status: 'declined' } } });
    render(<OfferModal offer={offerExpiringIn(30)} />);
    await userEvent.click(decline());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toBe('/api/offers/5/decline');
    expect(fetchMock.mock.calls[0][1].method).toBe('POST');
  });
});
