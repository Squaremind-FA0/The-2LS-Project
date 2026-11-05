import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { payruns } from '../../api/client.js';
import { useLoad } from '../../api/useLoad.js';

const dollars = (cents) => `$${(cents / 100).toFixed(2)}`;

// There is no way for a worker to list their own pay runs yet (PRP G6, known
// backend gap 2), so this asks for a reference rather than guessing ids.
function PayRunReference() {
  const navigate = useNavigate();
  const [reference, setReference] = useState('');
  return (
    <>
      <h1>My pay</h1>
      <p>
        Pay runs cannot be listed here yet. Please ask your company for a pay run reference, then
        enter it below.
      </p>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          navigate(`/worker/pay/${encodeURIComponent(reference.trim())}`);
        }}
      >
        <label>
          Pay run reference
          <input
            required
            inputMode="numeric"
            value={reference}
            onChange={(event) => setReference(event.target.value)}
          />
        </label>
        <button type="submit">Open</button>
      </form>
    </>
  );
}

function PayLines({ payrunId }) {
  const { data, error, loading } = useLoad(() => payruns.paylines(payrunId), payrunId);
  return (
    <>
      <h1>Pay run {payrunId}</h1>
      {error && <p role="alert">{error}</p>}
      {!loading && !error && data?.length === 0 && <p className="empty">No pay lines.</p>}
      {!error && data?.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Minutes worked</th>
                <th scope="col">Hourly rate</th>
                <th scope="col">Amount</th>
              </tr>
            </thead>
            <tbody>
              {data.map((line) => (
                <tr key={line.id}>
                  <td>{line.minutes_worked}</td>
                  <td>{dollars(line.hourly_rate_cents)}</td>
                  <td>{dollars(line.amount_cents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export function WorkerPayPage() {
  const { payrunId } = useParams();
  return payrunId ? <PayLines payrunId={payrunId} /> : <PayRunReference />;
}
