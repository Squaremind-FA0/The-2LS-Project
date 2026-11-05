import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ShareLocationToggle } from './ShareLocationToggle.jsx';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('ShareLocationToggle', () => {
  it('renders a labelled toggle wired to useShareLocation', async () => {
    const geolocation = { watchPosition: vi.fn(() => 7), clearWatch: vi.fn() };
    vi.stubGlobal('navigator', { ...navigator, geolocation });
    render(<ShareLocationToggle />);

    const toggle = screen.getByRole('switch', { name: 'Share my location' });
    expect(toggle).not.toBeChecked();

    await userEvent.click(toggle);
    expect(toggle).toBeChecked();
    expect(geolocation.watchPosition).toHaveBeenCalledTimes(1);

    await userEvent.click(toggle);
    expect(toggle).not.toBeChecked();
    expect(geolocation.clearWatch).toHaveBeenCalledWith(7);
  });

  it('shows why sharing could not start', async () => {
    vi.stubGlobal('navigator', {});
    render(<ShareLocationToggle />);
    await userEvent.click(screen.getByRole('switch', { name: 'Share my location' }));
    expect(screen.getByRole('alert')).toHaveTextContent(/not supported/i);
  });
});
