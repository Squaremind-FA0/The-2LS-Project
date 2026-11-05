import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { mockFetch } from '../../test/mockFetch.js';
import { renderWithAuth, USERS } from '../../test/renderWithAuth.jsx';
import { WorkerPayPage } from './WorkerPayPage.jsx';

const tree = (
  <Routes>
    <Route path="/worker/pay" element={<WorkerPayPage />} />
    <Route path="/worker/pay/:payrunId" element={<WorkerPayPage />} />
  </Routes>
);

const render = (route) => renderWithAuth(tree, { user: USERS.worker, route });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('WorkerPayPage', () => {
  it('WorkerPayPage with no payrunId route param shows the pay-run-discovery gap message instead of guessing an id', () => {
    const fetchMock = mockFetch({});
    render('/worker/pay');
    expect(screen.getByText(/ask your company for a pay run reference/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('opens the pay run a worker was given a reference for', async () => {
    mockFetch({ 'GET /payruns/7/paylines': { body: [] } });
    render('/worker/pay');
    await userEvent.type(screen.getByLabelText('Pay run reference'), '7');
    await userEvent.click(screen.getByRole('button', { name: 'Open' }));
    expect(await screen.findByRole('heading', { name: 'Pay run 7' })).toBeInTheDocument();
  });

  it('renders the paylines from GET /api/payruns/{id}/paylines with a payrunId route param', async () => {
    mockFetch({
      'GET /payruns/7/paylines': {
        body: [
          { id: 1, payrun_id: 7, worker_id: 11, minutes_worked: 90, hourly_rate_cents: 4000, amount_cents: 6000, created_at: '2026-11-05T10:00:00' },
        ],
      },
    });
    render('/worker/pay/7');
    const row = await screen.findByRole('row', { name: /90/ });
    expect(row).toHaveTextContent('$40.00');
    expect(row).toHaveTextContent('$60.00');
  });

  it('surfaces the 403 for a pay run with no lines for this worker', async () => {
    mockFetch({
      'GET /payruns/7/paylines': { status: 403, body: { detail: 'No pay lines for this worker in this payrun' } },
    });
    render('/worker/pay/7');
    expect(await screen.findByRole('alert')).toHaveTextContent('No pay lines for this worker in this payrun');
  });
});
