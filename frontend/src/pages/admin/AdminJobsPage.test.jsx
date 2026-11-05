import { fireEvent, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { mockFetch, sentBodies } from '../../test/mockFetch.js';
import { renderWithAuth, USERS } from '../../test/renderWithAuth.jsx';
import { AdminJobsPage } from './AdminJobsPage.jsx';

const job = (id, status) => ({
  id,
  trade: 'plumbing',
  address: `${id} Pipe St`,
  lat: -33.8,
  lng: 151.2,
  scheduled_for: '2026-11-06T08:00:00',
  status,
  assigned_worker_id: null,
  offer_round: 0,
  created_at: '2026-11-05T08:00:00',
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminJobsPage', () => {
  it('AdminJobsPage create form is hidden from company_admin and refetches the list after a dispatcher creates a job', async () => {
    const fetchMock = mockFetch({
      'GET /jobs': [{ body: [] }, { body: [job(1, 'draft')] }, { body: [job(1, 'draft')] }],
      'POST /jobs': { status: 201, body: job(1, 'draft') },
    });
    const admin = renderWithAuth(<AdminJobsPage />, { user: USERS.company_admin });
    await screen.findByText(/no jobs/i);
    expect(screen.queryByRole('button', { name: 'Add job' })).not.toBeInTheDocument();
    admin.unmount();

    renderWithAuth(<AdminJobsPage />, { user: USERS.dispatcher });
    await screen.findByRole('button', { name: 'Add job' });

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
        trade: 'plumbing',
        address: '1 Pipe St',
        lat: -33.8,
        lng: 151.2,
        scheduled_for: new Date('2026-11-06T08:00').toISOString(),
      },
    ]);
  });

  it('a dispatcher starts offering a draft job and the refetched status is shown', async () => {
    const fetchMock = mockFetch({
      'GET /jobs': [{ body: [job(1, 'draft')] }, { body: [job(1, 'offering')] }],
      'PATCH /jobs/1': { body: job(1, 'offering') },
    });
    renderWithAuth(<AdminJobsPage />, { user: USERS.dispatcher });
    const row = within(await screen.findByRole('listitem'));

    await userEvent.click(row.getByRole('button', { name: 'Start offering' }));

    expect(await within(screen.getByRole('listitem')).findByText('offering')).toBeInTheDocument();
    expect(sentBodies(fetchMock, 'PATCH /jobs/1')).toEqual([{ status: 'offering' }]);
    expect(screen.queryByRole('button', { name: 'Start offering' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel job' })).toBeInTheDocument();
  });

  it('offers no transition on a finished job, and none at all to a company_admin', async () => {
    mockFetch({ 'GET /jobs': { body: [job(1, 'completed'), job(2, 'draft')] } });
    const dispatcher = renderWithAuth(<AdminJobsPage />, { user: USERS.dispatcher });
    const rows = await screen.findAllByRole('listitem');
    expect(within(rows[0]).queryByRole('button')).not.toBeInTheDocument();
    expect(within(rows[1]).getAllByRole('button')).toHaveLength(2);
    dispatcher.unmount();

    renderWithAuth(<AdminJobsPage />, { user: USERS.company_admin });
    await screen.findAllByRole('listitem');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
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
});
