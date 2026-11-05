import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { mockFetch, sentBodies } from '../../test/mockFetch.js';
import { WorkerShiftsPage } from './WorkerShiftsPage.jsx';

const OVERLAP =
  'Shift overlaps existing shift for this worker (new.starts_at < existing.ends_at AND new.ends_at > existing.starts_at)';
const SHIFT = { id: 1, worker_id: 11, starts_at: '2026-11-05T09:00:00', ends_at: '2026-11-05T17:00:00', created_at: '2026-11-01T00:00:00' };

async function submitShift() {
  fireEvent.change(screen.getByLabelText('Starts'), { target: { value: '2026-11-05T10:00' } });
  fireEvent.change(screen.getByLabelText('Ends'), { target: { value: '2026-11-05T12:00' } });
  await userEvent.click(screen.getByRole('button', { name: 'Add shift' }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('WorkerShiftsPage', () => {
  it('WorkerShiftsPage surfaces the exact 409 overlap detail from the API', async () => {
    mockFetch({
      'GET /shifts': { body: [SHIFT] },
      'POST /shifts': { status: 409, body: { detail: OVERLAP } },
    });
    render(<WorkerShiftsPage />);
    await screen.findByRole('listitem');
    await submitShift();
    expect(await screen.findByRole('alert')).toHaveTextContent(OVERLAP);
  });

  it('sends the local form times as UTC and lists the new shift', async () => {
    const created = { ...SHIFT, id: 2, starts_at: '2026-11-06T10:00:00', ends_at: '2026-11-06T12:00:00' };
    const fetchMock = mockFetch({
      'GET /shifts': [{ body: [] }, { body: [created] }],
      'POST /shifts': { status: 201, body: created },
    });
    render(<WorkerShiftsPage />);
    expect(await screen.findByText(/no shifts/i)).toBeInTheDocument();

    await submitShift();

    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(1));
    expect(sentBodies(fetchMock, 'POST /shifts')).toEqual([
      {
        starts_at: new Date('2026-11-05T10:00').toISOString(),
        ends_at: new Date('2026-11-05T12:00').toISOString(),
      },
    ]);
  });

  it('shows a failure to load instead of an empty list', async () => {
    mockFetch({ 'GET /shifts': { status: 403, body: { detail: 'Insufficient role' } } });
    render(<WorkerShiftsPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Insufficient role');
    expect(screen.queryByText(/no shifts/i)).not.toBeInTheDocument();
  });
});
