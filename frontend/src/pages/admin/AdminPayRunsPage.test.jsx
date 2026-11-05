import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { mockFetch, sentBodies } from '../../test/mockFetch.js';
import { renderWithAuth, USERS } from '../../test/renderWithAuth.jsx';
import { AdminPayRunsPage } from './AdminPayRunsPage.jsx';

const payrun = (status, id = 3) => ({
  id,
  company_id: 1,
  period_start: '2026-11-01',
  period_end: '2026-11-07',
  status,
  created_at: '2026-11-08T00:00:00',
  updated_at: '2026-11-08T00:00:00',
});

const renderAdmin = () => renderWithAuth(<AdminPayRunsPage />, { user: USERS.company_admin });

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('AdminPayRunsPage', () => {
  it('AdminPayRunsPage transitions draft → approved → paid and blocks further edits with the 409 detail after paid', async () => {
    const fetchMock = mockFetch({
      'GET /payruns': { body: [payrun('draft')] },
      'PATCH /payruns/3': [
        { body: payrun('approved') },
        { body: payrun('paid') },
        { status: 409, body: { detail: 'Cannot edit a paid payrun' } },
      ],
    });
    renderAdmin();
    const row = within(await screen.findByRole('listitem'));
    expect(row.getByText('draft')).toBeInTheDocument();

    await userEvent.click(row.getByRole('button', { name: 'Approve' }));
    expect(await row.findByText('approved')).toBeInTheDocument();

    await userEvent.click(row.getByRole('button', { name: 'Mark paid' }));
    expect(await row.findByText('paid')).toBeInTheDocument();

    await userEvent.click(row.getByRole('button', { name: 'Mark paid' }));
    expect(await row.findByRole('alert')).toHaveTextContent('Cannot edit a paid payrun');
    expect(row.getByText('paid')).toBeInTheDocument();

    expect(sentBodies(fetchMock, 'PATCH /payruns/3')).toEqual([
      { status: 'approved' },
      { status: 'paid' },
      { status: 'paid' },
    ]);
  });

  it('the export control points at the CSV export endpoint', async () => {
    mockFetch({ 'GET /payruns': { body: [payrun('paid')] } });
    renderAdmin();
    const link = await screen.findByRole('link', { name: /export csv/i });
    expect(link).toHaveAttribute('href', '/api/payruns/3/export?format=csv');
  });

  it('clicking export sends the bearer token, because the endpoint rejects a plain navigation', async () => {
    localStorage.setItem('token', 'tok');
    const fetchMock = mockFetch({
      'GET /payruns': { body: [payrun('paid')] },
      'GET /payruns/3/export?format=csv': { body: 'worker_name,worker_email\n' },
    });
    const createObjectURL = vi.fn(() => 'blob:csv');
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() }));
    const save = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    renderAdmin();

    await userEvent.click(await screen.findByRole('link', { name: /export csv/i }));

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    const exportCall = fetchMock.mock.calls.find(([url]) => url.includes('/export'));
    expect(exportCall[1].headers.Authorization).toBe('Bearer tok');
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(save.mock.instances[0].download).toBe('payrun-3.csv');
  });

  it('surfaces a failed export', async () => {
    mockFetch({
      'GET /payruns': { body: [payrun('paid')] },
      'GET /payruns/3/export?format=csv': { status: 404, body: { detail: 'Payrun not found' } },
    });
    renderAdmin();
    await userEvent.click(await screen.findByRole('link', { name: /export csv/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Payrun not found');
  });

  it('creates a pay run and surfaces an overlapping period', async () => {
    const fetchMock = mockFetch({
      'GET /payruns': [{ body: [] }, { body: [payrun('draft')] }],
      'POST /payruns': [
        { status: 409, body: { detail: 'Pay period overlaps an existing payrun' } },
        { status: 201, body: payrun('draft') },
      ],
    });
    renderAdmin();
    await screen.findByText(/no pay runs/i);
    fireEvent.change(screen.getByLabelText('Period start'), { target: { value: '2026-11-01' } });
    fireEvent.change(screen.getByLabelText('Period end'), { target: { value: '2026-11-07' } });

    await userEvent.click(screen.getByRole('button', { name: 'Create pay run' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Pay period overlaps an existing payrun');

    await userEvent.click(screen.getByRole('button', { name: 'Create pay run' }));
    expect(await screen.findByRole('listitem')).toHaveTextContent('2026-11-01 to 2026-11-07');
    expect(sentBodies(fetchMock, 'POST /payruns')[1]).toEqual({
      period_start: '2026-11-01',
      period_end: '2026-11-07',
    });
  });

  it('shows a dispatcher a message and calls nothing', () => {
    const fetchMock = mockFetch({});
    renderWithAuth(<AdminPayRunsPage />, { user: USERS.dispatcher });
    expect(screen.getByText(/only company admins/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
