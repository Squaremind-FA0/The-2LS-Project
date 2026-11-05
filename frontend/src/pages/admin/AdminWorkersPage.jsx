import { useState } from 'react';

import { workers } from '../../api/client.js';
import { useLoad } from '../../api/useLoad.js';
import { useAuth } from '../../auth/AuthContext.jsx';

const EMPTY_FORM = { email: '', full_name: '', initial_password: '', trades: '', hourly_rate: '' };

function CreateWorkerForm({ onCreated }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const bind = (field) => ({
    value: form[field],
    onChange: (event) => setForm((current) => ({ ...current, [field]: event.target.value })),
  });

  async function handleSubmit(event) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await workers.create({
        ...form,
        trades: form.trades
          .split(',')
          .map((trade) => trade.trim())
          .filter(Boolean),
      });
      setForm(EMPTY_FORM);
      await onCreated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={handleSubmit}>
      <h2>Add a worker</h2>
      <label>
        Email
        <input type="email" required autoComplete="off" {...bind('email')} />
      </label>
      <label>
        Full name
        <input required autoComplete="off" {...bind('full_name')} />
      </label>
      <label>
        Initial password
        <input type="password" required minLength={12} autoComplete="new-password" {...bind('initial_password')} />
      </label>
      <label>
        Trades (comma separated)
        <input {...bind('trades')} />
      </label>
      <label>
        Hourly rate
        <input type="number" required min="0" step="0.01" {...bind('hourly_rate')} />
      </label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={busy}>
        Add worker
      </button>
    </form>
  );
}

function WorkerRow({ worker }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  async function viewDetails() {
    setError(null);
    try {
      setDetail(await workers.get(worker.id));
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <li className="card">
      <h2>{worker.full_name}</h2>
      <p>{worker.email}</p>
      {detail && (
        <>
          <p>{`$${detail.worker_profile.hourly_rate}/hour`}</p>
          <p>{`Trades: ${detail.worker_profile.trades.join(', ') || 'none'}`}</p>
          <p>{detail.is_active ? 'Active' : 'Inactive'}</p>
        </>
      )}
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        <button type="button" className="secondary" onClick={viewDetails}>
          View details
        </button>
      </div>
    </li>
  );
}

export function AdminWorkersPage() {
  const { user } = useAuth();
  const { data, error, loading, reload } = useLoad(() => workers.list());
  return (
    <>
      <h1>Workers</h1>
      {user.role === 'company_admin' && <CreateWorkerForm onCreated={reload} />}
      {error && <p role="alert">{error}</p>}
      {!loading && !error && data?.length === 0 && <p className="empty">No workers yet.</p>}
      {data?.length > 0 && (
        <ul className="card-list">
          {data.map((worker) => (
            <WorkerRow key={worker.id} worker={worker} />
          ))}
        </ul>
      )}
    </>
  );
}
