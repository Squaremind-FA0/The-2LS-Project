import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { mockFetch } from '../../test/mockFetch.js';
import { AdminRosterPage } from './AdminRosterPage.jsx';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminRosterPage', () => {
  it('AdminRosterPage renders the documented backend-gap message', () => {
    const fetchMock = mockFetch({});
    render(<AdminRosterPage />);
    expect(screen.getByRole('heading', { name: 'Roster' })).toBeInTheDocument();
    expect(screen.getByText(/company-wide roster is not available yet/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
