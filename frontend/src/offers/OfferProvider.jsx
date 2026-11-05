import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { offers } from '../api/client.js';
import { parseUtc } from '../api/time.js';
import { useAuth } from '../auth/AuthContext.jsx';
import { loadJobs, saveJobs } from './myJobs.js';
import { OfferModal } from './OfferModal.jsx';

const OfferContext = createContext(null);

// The server said this socket's identity is not acceptable, so retrying is
// pointless. Every other close, including a dropped network (1006), reconnects.
const FATAL_CLOSE_CODES = new Set([4401, 4403]);
const BACKOFF_BASE_MS = 1000;
const BACKOFF_MAX_MS = 30000;

export function useOffers() {
  const value = useContext(OfferContext);
  if (!value) {
    throw new Error('useOffers must be used inside an OfferProvider');
  }
  return value;
}

function workerSocketUrl() {
  const { protocol, host } = window.location;
  return `${protocol === 'https:' ? 'wss' : 'ws'}://${host}/api/ws/worker`;
}

const bySoonestExpiry = (a, b) => parseUtc(a.expires_at) - parseUtc(b.expires_at);

export function OfferProvider({ children }) {
  const { token, user, logout } = useAuth();
  const [queue, setQueue] = useState([]);
  const [notice, setNotice] = useState('');
  const [jobs, setJobs] = useState(() => (user ? loadJobs(user.id) : []));
  // Closed for this session, whatever a slower in-flight poll may still say.
  const closed = useRef(new Set());

  const dropOffer = useCallback((offerId) => {
    closed.current.add(offerId);
    setQueue((current) => current.filter((offer) => offer.offer_id !== offerId));
  }, []);

  useEffect(() => {
    if (user) {
      saveJobs(user.id, jobs);
    }
  }, [user, jobs]);

  useEffect(() => {
    if (!token) {
      return undefined;
    }
    let socket = null;
    let retryTimer = null;
    let attempts = 0;
    let disposed = false;

    function connect() {
      const ws = new WebSocket(workerSocketUrl());
      socket = ws;
      // Offers pushed on this connection since auth.ok. A poll can't vouch for
      // them, since they may be newer than its snapshot.
      let pushed = new Set();

      async function poll() {
        try {
          const pending = await offers.pending();
          if (disposed || ws !== socket) {
            return;
          }
          // The poll is current truth for everything except what was pushed
          // after it started, which also clears offers closed during an outage.
          setQueue((current) => {
            const merged = new Map(pending.map((offer) => [offer.offer_id, offer]));
            for (const offer of current) {
              if (pushed.has(offer.offer_id)) {
                merged.set(offer.offer_id, offer);
              }
            }
            return [...merged.values()]
              .filter((offer) => !closed.current.has(offer.offer_id))
              .sort(bySoonestExpiry);
          });
        } catch {
          // Pushes still arrive, and the next reconnect polls again. A rejected
          // token has already ended the session through apiFetch.
        }
      }

      ws.onopen = () => ws.send(JSON.stringify({ type: 'auth', token }));

      ws.onmessage = ({ data }) => {
        if (disposed || ws !== socket) {
          return;
        }
        let frame;
        try {
          frame = JSON.parse(data);
        } catch {
          return;
        }
        switch (frame.type) {
          case 'auth.ok':
            attempts = 0;
            pushed = new Set();
            poll();
            break;
          case 'ping':
            ws.send(JSON.stringify({ type: 'pong' }));
            break;
          case 'offer.created': {
            if (closed.current.has(frame.offer_id)) {
              break;
            }
            pushed.add(frame.offer_id);
            setQueue((current) =>
              [...current.filter((offer) => offer.offer_id !== frame.offer_id), frame].sort(bySoonestExpiry),
            );
            break;
          }
          case 'offer.expired':
          case 'offer.taken':
            dropOffer(frame.offer_id);
            break;
          default:
            break;
        }
      };

      ws.onclose = ({ code }) => {
        if (disposed || ws !== socket) {
          return;
        }
        if (FATAL_CLOSE_CODES.has(code)) {
          logout();
          return;
        }
        const delay = Math.min(BACKOFF_BASE_MS * 2 ** attempts, BACKOFF_MAX_MS);
        attempts += 1;
        retryTimer = setTimeout(connect, delay);
      };
    }

    connect();
    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      socket?.close();
    };
  }, [token, logout, dropOffer]);

  const accept = useCallback(
    async (offer) => {
      await offers.accept(offer.offer_id);
      dropOffer(offer.offer_id);
      setJobs((current) =>
        current.some((job) => job.id === offer.job.id)
          ? current
          : [...current, { ...offer.job, status: 'assigned' }],
      );
      setNotice('Job accepted. It is now in your jobs list.');
    },
    [dropOffer],
  );

  const decline = useCallback(
    async (offer) => {
      await offers.decline(offer.offer_id);
      dropOffer(offer.offer_id);
      setNotice('');
    },
    [dropOffer],
  );

  const handleUnavailable = useCallback(
    (offer, message) => {
      dropOffer(offer.offer_id);
      setNotice(message);
    },
    [dropOffer],
  );

  const handleDismiss = useCallback((offer) => dropOffer(offer.offer_id), [dropOffer]);

  const setJobStatus = useCallback((jobId, status) => {
    setJobs((current) => current.map((job) => (job.id === jobId ? { ...job, status } : job)));
  }, []);

  const activeOffer = queue[0] ?? null;
  const value = useMemo(
    () => ({ queue, activeOffer, accept, decline, jobs, setJobStatus }),
    [queue, activeOffer, accept, decline, jobs, setJobStatus],
  );

  return (
    <OfferContext.Provider value={value}>
      <div className="notice" role="status">
        {notice}
      </div>
      {activeOffer && (
        <OfferModal
          key={activeOffer.offer_id}
          offer={activeOffer}
          onAccept={accept}
          onDecline={decline}
          onUnavailable={handleUnavailable}
          onDismiss={handleDismiss}
        />
      )}
      {children}
    </OfferContext.Provider>
  );
}
