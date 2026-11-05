import { useState } from 'react';

import { shifts } from '../../api/client.js';
import { formatUtc, localInputToUtcIso } from '../../api/time.js';
import { useLoad } from '../../api/useLoad.js';

export function WorkerShiftsPage() {
  const { data, error: loadError, loading, reload } = useLoad(() => shifts.list());
  const [startsAt, setStartsAt] = useState('');
  const [endsAt, setEndsAt] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await shifts.create({
        starts_at: localInputToUtcIso(startsAt),
        ends_at: localInputToUtcIso(endsAt),
      });
      setStartsAt('');
      setEndsAt('');
      await reload();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>My shifts</h1>
      <form className="form" onSubmit={handleSubmit}>
        <label>
          Starts
          <input
            type="datetime-local"
            required
            value={startsAt}
            onChange={(event) => setStartsAt(event.target.value)}
          />
        </label>
        <label>
          Ends
          <input
            type="datetime-local"
            required
            value={endsAt}
            onChange={(event) => setEndsAt(event.target.value)}
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={busy}>
          Add shift
        </button>
      </form>
      {loadError && <p role="alert">{loadError}</p>}
      {!loading && !loadError && data?.length === 0 && <p className="empty">No shifts yet.</p>}
      {data?.length > 0 && (
        <ul className="card-list">
          {data.map((shift) => (
            <li key={shift.id} className="card">
              {formatUtc(shift.starts_at)} to {formatUtc(shift.ends_at)}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
