import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { mockFetch, sentBodies } from '../../test/mockFetch.js';
import { renderWithAuth, USERS } from '../../test/renderWithAuth.jsx';
import { AdminWorkersPage } from './AdminWorkersPage.jsx';

const worker = (id, name) => ({
  id,
  email: `${name.toLowerCase()}@example.com`,
  full_name: name,
  role: 'worker',
  company_id: 1,
  is_active: true,
  worker_profile: { trades: ['plumbing'], hourly_rate: '35.50', active: true },
});

async function fillForm() {
  await userEvent.type(screen.getByLabelText('Email'), 'new@example.com');
  await userEvent.type(screen.getByLabelText('Full name'), 'New Worker');
  await userEvent.type(screen.getByLabelText('Initial password'), 'long-enough-password');
  await userEvent.type(screen.getByLabelText('Trades (comma separated)'), 'plumbing, electrical');
  await userEvent.type(screen.getByLabelText('Hourly rate'), '42.75');
  await userEvent.click(screen.getByRole('button', { name: 'Add worker' }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminWorkersPage', () => {
  it('company_admin creates a worker and it appears in the list', async () => {
    const fetchMock = mockFetch({
      'GET /workers': [{ body: [worker(1, 'Old')] }, { body: [worker(1, 'Old'), worker(2, 'New')] }],
      'POST /workers': { status: 201, body: worker(2, 'New') },
    });
    renderWithAuth(<AdminWorkersPage />, { user: USERS.company_admin });
    await screen.findByText('Old');

    await fillForm();

    expect(await screen.findByText('New')).toBeInTheDocument();
    expect(sentBodies(fetchMock, 'POST /workers')).toEqual([
      {
        email: 'new@example.com',
        full_name: 'New Worker',
        initial_password: 'long-enough-password',
        trades: ['plumbing', 'electrical'],
        hourly_rate: '42.75',
      },
    ]);
    expect(screen.getByLabelText('Initial password')).toHaveValue('');
  });

  it('AdminWorkersPage hides the create form from a dispatcher and surfaces 409 duplicate-email', async () => {
    mockFetch({
      'GET /workers': { body: [worker(1, 'Old')] },
      'POST /workers': { status: 409, body: { detail: 'Email already registered' } },
    });
    const dispatcher = renderWithAuth(<AdminWorkersPage />, { user: USERS.dispatcher });
    await screen.findByText('Old');
    expect(screen.queryByRole('button', { name: 'Add worker' })).not.toBeInTheDocument();
    dispatcher.unmount();

    renderWithAuth(<AdminWorkersPage />, { user: USERS.company_admin });
    await screen.findByText('Old');
    await fillForm();
    expect(await screen.findByRole('alert')).toHaveTextContent('Email already registered');
  });

  it('has no edit or delete controls, and shows a worker from GET /api/workers/{id}', async () => {
    const fetchMock = mockFetch({
      'GET /workers': { body: [worker(1, 'Old')] },
      'GET /workers/1': { body: worker(1, 'Old') },
    });
    renderWithAuth(<AdminWorkersPage />, { user: USERS.dispatcher });
    const row = within(await screen.findByRole('listitem'));
    expect(row.getAllByRole('button').map((button) => button.textContent)).toEqual(['View details']);

    await userEvent.click(row.getByRole('button', { name: 'View details' }));

    await waitFor(() => expect(row.getByText('$35.50/hour')).toBeInTheDocument());
    expect(row.getByText('Trades: plumbing')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/workers/1')).toBe(true);
  });

  it('shows a failure to load instead of an empty list', async () => {
    mockFetch({ 'GET /workers': { status: 403, body: { detail: 'Insufficient role' } } });
    renderWithAuth(<AdminWorkersPage />, { user: USERS.dispatcher });
    expect(await screen.findByRole('alert')).toHaveTextContent('Insufficient role');
    expect(screen.queryByText(/no workers/i)).not.toBeInTheDocument();
  });
});
