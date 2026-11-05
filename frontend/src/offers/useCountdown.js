import { useEffect, useState } from 'react';

/**
 * Whole seconds until `targetDate`, floored at 0. Derived from the clock on
 * each tick rather than decremented, because browsers throttle timers in
 * background tabs and a counted-down value would drift.
 */
export function useCountdown(targetDate) {
  const targetMs = targetDate.getTime();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    setNow(Date.now());
    if (targetMs <= Date.now()) {
      return undefined;
    }
    const timer = setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= targetMs) {
        clearInterval(timer);
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [targetMs]);

  const secondsLeft = Math.max(0, Math.ceil((targetMs - now) / 1000));
  return { secondsLeft, isExpired: secondsLeft === 0 };
}
