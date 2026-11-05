import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Runs `load` on mount and whenever `key` changes, keeping only the latest
 * answer. `reload` re-runs it after a mutation. A failure is reported rather
 * than left to look like an empty result.
 */
export function useLoad(load, key = null) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const latest = useRef(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(async () => {
    latest.current += 1;
    const run = latest.current;
    try {
      const data = await loadRef.current();
      if (run === latest.current) {
        setState({ data, error: null, loading: false });
      }
    } catch (err) {
      if (run === latest.current) {
        setState((current) => ({ ...current, error: err.message, loading: false }));
      }
    }
  }, []);

  useEffect(() => {
    setState((current) => ({ ...current, loading: true }));
    reload();
    return () => {
      latest.current += 1;
    };
  }, [reload, key]);

  return { ...state, reload };
}
