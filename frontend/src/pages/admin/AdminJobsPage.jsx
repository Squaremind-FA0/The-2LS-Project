import { useState } from 'react';

import { jobs } from '../../api/client.js';
import { formatUtc, localInputToUtcIso } from '../../api/time.js';
import { useLoad } from '../../api/useLoad.js';
import { useAuth } from '../../auth/AuthContext.jsx';

const EMPTY_FORM = { trade: '', address: '', lat: '', lng: '', scheduled_for: '' };

// Mirrors MANUAL_TRANSITIONS in backend/app/services/dispatch.py for the two
// targets this page drives. The server still decides, and refusals are shown.
const OFFERING = { label: 'Start offering', status: 'offering' };
const CANCEL = { label: 'Cancel job', status: 'cancelled' };
const TRANSITIONS = {
  draft: [OFFERING, CANCEL],
  offering: [CANCEL],
  assigned: [CANCEL],
  in_progress: [CANCEL],
};

function CreateJobForm({ onCreated }) {
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
      await jobs.create({
        trade: form.trade,
        address: form.address,
        lat: Number(form.lat),
        lng: Number(form.lng),
        scheduled_for: localInputToUtcIso(form.scheduled_for),
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
      <h2>Add a job</h2>
      <label>
        Trade
        <input required {...bind('trade')} />
      </label>
      <label>
        Address
        <input required {...bind('address')} />
      </label>
      <label>
        Latitude
        <input type="number" required min="-90" max="90" step="any" {...bind('lat')} />
      </label>
      <label>
        Longitude
        <input type="number" required min="-180" max="180" step="any" {...bind('lng')} />
      </label>
      <label>
        Scheduled for
        <input type="datetime-local" required {...bind('scheduled_for')} />
      </label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={busy}>
        Add job
      </button>
    </form>
  );
}

function JobRow({ job, canTransition, onChanged }) {
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function transition(status) {
    setError(null);
    setBusy(true);
    try {
      await jobs.transition(job.id, status);
      await onChanged();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const actions = canTransition ? (TRANSITIONS[job.status] ?? []) : [];
  return (
    <li className="card">
      <h2>{job.trade}</h2>
      <p>{job.address}</p>
      <p>Scheduled: {formatUtc(job.scheduled_for)}</p>
      <p>
        Status: <span className="status">{job.status}</span>
      </p>
      {error && <p role="alert">{error}</p>}
      {actions.length > 0 && (
        <div className="actions">
          {actions.map(({ label, status }) => (
            <button key={status} type="button" className={status === 'cancelled' ? 'secondary' : undefined} disabled={busy} onClick={() => transition(status)}>
              {label}
            </button>
          ))}
        </div>
      )}
    </li>
  );
}

export function AdminJobsPage() {
  const { user } = useAuth();
  const isDispatcher = user.role === 'dispatcher';
  const { data, error, loading, reload } = useLoad(() => jobs.list());
  return (
    <>
      <h1>Jobs</h1>
      {isDispatcher && <CreateJobForm onCreated={reload} />}
      {error && <p role="alert">{error}</p>}
      {!loading && !error && data?.length === 0 && <p className="empty">No jobs yet.</p>}
      {data?.length > 0 && (
        <ul className="card-list">
          {data.map((job) => (
            <JobRow key={job.id} job={job} canTransition={isDispatcher} onChanged={reload} />
          ))}
        </ul>
      )}
    </>
  );
}
