import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import App from './App.jsx';

describe('App', () => {
  it('redirects / to the login placeholder', () => {
    render(<App />);
    expect(screen.getByText('Login')).toBeInTheDocument();
  });
});
