import { useCallback, useEffect, useRef, useState } from 'react';

import { location as locationApi } from '../api/client.js';

export function useShareLocation() {
  const [sharing, setSharing] = useState(false);
  const [error, setError] = useState(null);
  const watchId = useRef(null);

  const stop = useCallback(() => {
    if (watchId.current !== null) {
      navigator.geolocation.clearWatch(watchId.current);
      watchId.current = null;
    }
    setSharing(false);
  }, []);

  const start = useCallback(() => {
    if (!navigator.geolocation) {
      setError('Location sharing is not supported on this device.');
      return;
    }
    setError(null);
    setSharing(true);
    watchId.current = navigator.geolocation.watchPosition(
      ({ coords }) => {
        locationApi
          .update(coords.latitude, coords.longitude)
          .then(() => setError(null))
          .catch((err) => setError(err.message));
      },
      (err) => {
        if (err.code === err.PERMISSION_DENIED) {
          stop();
          setError('Location permission was denied. Allow it in your browser to share your location.');
        } else {
          setError('Could not read your location. Still trying.');
        }
      },
    );
  }, [stop]);

  const toggle = useCallback(() => {
    if (watchId.current === null) {
      start();
    } else {
      stop();
    }
  }, [start, stop]);

  useEffect(
    () => () => {
      if (watchId.current !== null) {
        navigator.geolocation.clearWatch(watchId.current);
        watchId.current = null;
      }
    },
    [],
  );

  return { sharing, error, toggle };
}
