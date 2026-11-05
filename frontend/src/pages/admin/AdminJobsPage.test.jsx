import { fireEvent, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { mockFetch, sentBodies } from '../../test/mockFetch.js';
import { renderWithAuth, USERS } from '../../test/renderWithAuth.jsx';
import { AdminJobsPage } from './AdminJobsPage.jsx';

const job = (id, status, version = 1) => ({
  id,
  title: `Job ${id}`,
  description: '',
  trade: 'plumbing',
  address: `${id} Pipe St`,
  lat: -33.8,
  lng: 151.2,
  scheduled_for: '2026-11-06T08:00:00',
  status,
  assigned_worker_id: null,
  offer_round: 0,
  created_by: 1,
  version,
  created_at: '2026-11-05T08:00:00',
  updated_at: '2026-11-05T08:00:00',
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminJobsPage', () => {
  it.each(['dispatcher', 'company_admin'])('%s creates a job and the list is refetched', async (role) => {
    const fetchMock = mockFetch({
      'GET /jobs': [{ body: [] }, { body: [job(1, 'draft')] }],
      'POST /jobs': { status: 201, body: job(1, 'draft') },
    });
    renderWithAuth(<AdminJobsPage />, { user: USERS[role] });
    await screen.findByRole('button', { name: 'Add job' });

    await userEvent.type(screen.getByLabelText('Title'), 'Fix tap');
    await userEvent.type(screen.getByLabelText('Trade'), 'plumbing');
    await userEvent.type(screen.getByLabelText('Address'), '1 Pipe St');
    await userEvent.type(screen.getByLabelText('Latitude'), '-33.8');
    await userEvent.type(screen.getByLabelText('Longitude'), '151.2');
    fireEvent.change(screen.getByLabelText('Scheduled for'), { target: { value: '2026-11-06T08:00' } });
    await userEvent.click(screen.getByRole('button', { name: 'Add job' }));

    const row = within(await screen.findByRole('listitem'));
    expect(row.getByText('draft')).toBeInTheDocument();
    expect(sentBodies(fetchMock, 'POST /jobs')).toEqual([
      {
        title: 'Fix tap',
        trade: 'plumbing',
        address: '1 Pipe St',
        lat: -33.8,
        lng: 151.2,
        scheduled_for: new Date('2026-11-06T08:00').toISOString(),
      },
    ]);
  });

  it('starts dispatching a draft job, sending the version it was shown, and shows the refetched status', async () => {
    const fetchMock = mockFetch({
      'GET /jobs': [{ body: [job(1, 'draft', 4)] }, { body: [job(1, 'dispatching', 5)] }],
      'PATCH /jobs/1': { body: job(1, 'dispatching', 5) },
    });
    renderWithAuth(<AdminJobsPage />, { user: USERS.dispatcher });
    const row = within(await screen.findByRole('listitem'));

    await userEvent.click(row.getByRole('button', { name: 'Start dispatching' }));

    expect(await within(screen.getByRole('listitem')).findByText('dispatching')).toBeInTheDocument();
    expect(sentBodies(fetchMock, 'PATCH /jobs/1')).toEqual([{ version: 4, status: 'dispatching' }]);
    expect(screen.queryByRole('button', { name: 'Start dispatching' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel job' })).toBeInTheDocument();
  });

  it.each(['dispatcher', 'company_admin'])('offers %s no transition on a finished job', async (role) => {
    mockFetch({ 'GET /jobs': { body: [job(1, 'completed'), job(2, 'draft')] } });
    renderWithAuth(<AdminJobsPage />, { user: USERS[role] });
    const rows = await screen.findAllByRole('listitem');
    expect(within(rows[0]).queryByRole('button')).not.toBeInTheDocument();
    expect(within(rows[1]).getAllByRole('button')).toHaveLength(2);
  });

  it('shows the title, and says so when a job has no schedule', async () => {
    mockFetch({ 'GET /jobs': { body: [{ ...job(1, 'draft'), scheduled_for: null }] } });
    renderWithAuth(<AdminJobsPage />, { user: USERS.dispatcher });
    const row = within(await screen.findByRole('listitem'));
    expect(row.getByRole('heading', { name: 'Job 1' })).toBeInTheDocument();
    expect(row.getByText('plumbing')).toBeInTheDocument();
    expect(row.getByText('Scheduled: Not scheduled')).toBeInTheDocument();
  });

  it('surfaces a refused transition', async () => {
    mockFetch({
      'GET /jobs': { body: [job(1, 'draft')] },
      'PATCH /jobs/1': { status: 400, body: { detail: 'Cannot change job status from draft to cancelled' } },
    });
    renderWithAuth(<AdminJobsPage />, { user: USERS.dispatcher });
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel job' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot change job status from draft to cancelled');
  });

  it('reloads the list after a stale-version refusal so the next try uses the current version', async () => {
    const fetchMock = mockFetch({
      'GET /jobs': [{ body: [job(1, 'draft', 1)] }, { body: [job(1, 'draft', 2)] }],
      'PATCH /jobs/1': { status: 409, body: { detail: 'Job was changed by someone else; reload and try again' } },
    });
    renderWithAuth(<AdminJobsPage />, { user: USERS.dispatcher });
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel job' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('changed by someone else');
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => (init?.method ?? 'GET') === 'GET').length).toBe(2));
  });
});
