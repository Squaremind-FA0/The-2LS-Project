import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { AuthContext } from '../auth/AuthContext.jsx';

export const USERS = {
  worker: { id: 11, email: 'w@example.com', full_name: 'Wes Worker', role: 'worker', company_id: 1 },
  company_admin: { id: 12, email: 'a@example.com', full_name: 'Ada Admin', role: 'company_admin', company_id: 1 },
  dispatcher: { id: 13, email: 'd@example.com', full_name: 'Dee Dispatch', role: 'dispatcher', company_id: 1 },
};

/** Render `ui` inside a controlled auth context and a MemoryRouter. */
export function renderWithAuth(ui, { user = null, route = '/', auth = {} } = {}) {
  const token = user ? 'test-token' : null;
  const value = { token, user, loading: false, login: async () => {}, logout: () => {}, ...auth };
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
    </AuthContext.Provider>,
  );
}
