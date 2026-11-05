import { useState } from 'react';

import { payruns } from '../../api/client.js';
import { useLoad } from '../../api/useLoad.js';
import { useAuth } from '../../auth/AuthContext.jsx';

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function CreatePayRunForm({ onCreated }) {
  const [periodStart, setPeriodStart] = useState('');
  const [periodEnd, setPeriodEnd] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await payruns.create({ period_start: periodStart, period_end: periodEnd });
      setPeriodStart('');
      setPeriodEnd('');
      await onCreated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={handleSubmit}>
      <h2>Create a pay run</h2>
      <label>
        Period start
        <input type="date" required value={periodStart} onChange={(event) => setPeriodStart(event.target.value)} />
      </label>
      <label>
        Period end
        <input type="date" required value={periodEnd} onChange={(event) => setPeriodEnd(event.target.value)} />
      </label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={busy}>
        Create pay run
      </button>
    </form>
  );
}

function PayRunRow({ initial }) {
  const [payrun, setPayrun] = useState(initial);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function transition(status) {
    setError(null);
    setBusy(true);
    try {
      setPayrun(await payruns.transition(payrun.id, status));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  // The export endpoint requires the bearer token, which a browser navigation
  // cannot send, so a plain link would always get a 401.
  async function exportCsv(event) {
    event.preventDefault();
    setError(null);
    try {
      saveBlob(await payruns.exportCsv(payrun.id), `payrun-${payrun.id}.csv`);
    } catch (err) {
      setError(err.message);
    }
  }

  // Mark paid stays available after paid on purpose: the server owns the
  // transition rules, and its 409 is what tells the user the run is locked.
  return (
    <li className="card">
      <h2>
        {payrun.period_start} to {payrun.period_end}
      </h2>
      <p>Reference: {payrun.id}</p>
      <p>
        Status: <span className="status">{payrun.status}</span>
      </p>
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        {payrun.status === 'draft' && (
          <button type="button" disabled={busy} onClick={() => transition('approved')}>
            Approve
          </button>
        )}
        <button type="button" disabled={busy} onClick={() => transition('paid')}>
          Mark paid
        </button>
        <a href={payruns.exportUrl(payrun.id)} onClick={exportCsv}>
          Export CSV
        </a>
      </div>
    </li>
  );
}

function PayRuns() {
  const { data, error, loading, reload } = useLoad(() => payruns.list());
  return (
    <>
      <CreatePayRunForm onCreated={reload} />
      {error && <p role="alert">{error}</p>}
      {!loading && !error && data?.length === 0 && <p className="empty">No pay runs yet.</p>}
      {data?.length > 0 && (
        <ul className="card-list">
          {data.map((payrun) => (
            <PayRunRow key={payrun.id} initial={payrun} />
          ))}
        </ul>
      )}
    </>
  );
}

export function AdminPayRunsPage() {
  const { user } = useAuth();
  return (
    <>
      <h1>Pay runs</h1>
      {user.role === 'company_admin' ? <PayRuns /> : <p>Only company admins can manage pay runs.</p>}
    </>
  );
}
