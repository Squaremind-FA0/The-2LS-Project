import { NavLink } from 'react-router-dom';

import { useAuth } from '../auth/AuthContext.jsx';

const WORKER_LINKS = [
  { to: '/worker/jobs', label: 'Jobs' },
  { to: '/worker/shifts', label: 'Shifts' },
  { to: '/worker/pay', label: 'Pay' },
];

const DISPATCHER_LINKS = [
  { to: '/admin/workers', label: 'Workers' },
  { to: '/admin/jobs', label: 'Jobs' },
  { to: '/admin/roster', label: 'Roster' },
];

const LINKS_BY_ROLE = {
  worker: WORKER_LINKS,
  dispatcher: DISPATCHER_LINKS,
  company_admin: [...DISPATCHER_LINKS, { to: '/admin/payruns', label: 'PayRuns' }],
};

export function Nav() {
  const { user, logout } = useAuth();
  const links = LINKS_BY_ROLE[user?.role] ?? [];
  return (
    <nav className="nav" aria-label="Main">
      <ul>
        {links.map(({ to, label }) => (
          <li key={to}>
            <NavLink to={to}>{label}</NavLink>
          </li>
        ))}
      </ul>
      <button type="button" onClick={logout}>
        Log out
      </button>
    </nav>
  );
}
