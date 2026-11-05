import { useState } from 'react';

import { jobs as jobsApi } from '../../api/client.js';
import { formatUtc } from '../../api/time.js';
import { useOffers } from '../../offers/OfferProvider.jsx';

const NEXT_STEP = {
  assigned: { label: 'Start', call: jobsApi.clockIn, status: 'in_progress' },
  in_progress: { label: 'Complete', call: jobsApi.clockOut, status: 'completed' },
};

function JobCard({ job, onStatus }) {
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const step = NEXT_STEP[job.status];

  async function advance() {
    setError(null);
    setBusy(true);
    try {
      await step.call(job.id);
      onStatus(job.id, step.status);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="card">
      <h2>{job.trade}</h2>
      <p>{job.address}</p>
      <p>Scheduled: {formatUtc(job.scheduled_for)}</p>
      <p>
        Status: <span className="status">{job.status}</span>
      </p>
      {error && <p role="alert">{error}</p>}
      {step && (
        <div className="actions">
          <button type="button" disabled={busy} onClick={advance}>
            {step.label}
          </button>
        </div>
      )}
    </li>
  );
}

export function WorkerJobsPage() {
  const { jobs, setJobStatus } = useOffers();
  return (
    <>
      <h1>My jobs</h1>
      {jobs.length === 0 ? (
        <p className="empty">
          No jobs yet. This list only shows jobs you accepted on this device, so it starts empty on a
          new device or after clearing your browser.
        </p>
      ) : (
        <ul className="card-list">
          {jobs.map((job) => (
            <JobCard key={job.id} job={job} onStatus={setJobStatus} />
          ))}
        </ul>
      )}
    </>
  );
}
