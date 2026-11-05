import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client.js';
import { renderWithAuth } from '../test/renderWithAuth.jsx';
import { Login } from './Login.jsx';

async function submit() {
  await userEvent.type(screen.getByLabelText('Email'), 'w@example.com');
  await userEvent.type(screen.getByLabelText('Password'), 'secret-password');
  await userEvent.click(screen.getByRole('button', { name: 'Log in' }));
}

describe('Login', () => {
  it('Login calls useAuth().login on submit and shows "Invalid credentials" on 401', async () => {
    const login = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(401, 'Invalid credentials'))
      .mockResolvedValueOnce(undefined);
    renderWithAuth(<Login />, { auth: { login } });

    await submit();

    expect(login).toHaveBeenCalledWith('w@example.com', 'secret-password');
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid credentials');
    expect(screen.getByRole('heading', { name: 'Log in' })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Log in' }));
    expect(login).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
