import { useEffect, useId, useRef, useState } from 'react';

import { ApiError, offers } from '../api/client.js';
import { formatUtc, parseUtc } from '../api/time.js';
import { useCountdown } from './useCountdown.js';
import { useFocusTrap } from './useFocusTrap.js';

export const UNAVAILABLE_MESSAGE = 'This job is no longer available.';

// The server is authoritative on a race: not found, already taken or resolved,
// and expired all mean this offer can no longer be acted on.
const UNAVAILABLE_STATUSES = new Set([404, 409, 410]);

const postAccept = (offer) => offers.accept(offer.offer_id);
const postDecline = (offer) => offers.decline(offer.offer_id);

export function OfferModal({
  offer,
  onAccept = postAccept,
  onDecline = postDecline,
  onUnavailable,
  onDismiss,
}) {
  const { secondsLeft, isExpired } = useCountdown(parseUtc(offer.expires_at));
  const [error, setError] = useState(null);
  const dialogRef = useRef(null);
  const dismissRef = useRef(null);
  // A ref, not a disabled attribute: disabling the focused button would drop
  // keyboard focus out of the dialog for the length of the request.
  const inFlight = useRef(false);
  const titleId = useId();
  useFocusTrap(dialogRef);

  // Accept and Decline just went disabled, so focus has nowhere to stay.
  useEffect(() => {
    if (isExpired) {
      dismissRef.current?.focus();
    }
  }, [isExpired]);

  async function run(action) {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setError(null);
    try {
      await action(offer);
    } catch (err) {
      if (err instanceof ApiError && UNAVAILABLE_STATUSES.has(err.status)) {
        onUnavailable?.(offer, UNAVAILABLE_MESSAGE);
      } else {
        setError(err.message);
      }
    } finally {
      inFlight.current = false;
    }
  }

  const { job, distance_km: distanceKm } = offer;
  return (
    <div className="modal-backdrop">
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <h2 id={titleId}>Job offer</h2>
        <dl>
          <dt>Trade</dt>
          <dd>{job.trade}</dd>
          <dt>Address</dt>
          <dd>{job.address}</dd>
          <dt>Scheduled</dt>
          <dd>{formatUtc(job.scheduled_for) || 'Not scheduled'}</dd>
          <dt>Distance</dt>
          <dd>{distanceKm === null ? 'Distance unknown' : `${distanceKm.toFixed(1)} km away`}</dd>
        </dl>
        <p role="timer">{isExpired ? 'This offer has expired' : `Time left: ${secondsLeft}s`}</p>
        {error && <p role="alert">{error}</p>}
        <div className="actions">
          <button type="button" disabled={isExpired} onClick={() => run(onAccept)}>
            Accept job
          </button>
          <button type="button" disabled={isExpired} onClick={() => run(onDecline)}>
            Decline job
          </button>
          {isExpired && (
            <button ref={dismissRef} type="button" onClick={() => onDismiss?.(offer)}>
              Dismiss
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
