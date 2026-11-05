import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useCountdown } from './useCountdown.js';

const NOW = new Date('2026-11-05T10:00:00Z');

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useCountdown', () => {
  it('useCountdown ticks down and reports isExpired at zero', () => {
    const target = new Date(NOW.getTime() + 3000);
    const { result } = renderHook(() => useCountdown(target));
    expect(result.current).toEqual({ secondsLeft: 3, isExpired: false });

    act(() => vi.advanceTimersByTime(1000));
    expect(result.current).toEqual({ secondsLeft: 2, isExpired: false });

    act(() => vi.advanceTimersByTime(2000));
    expect(result.current).toEqual({ secondsLeft: 0, isExpired: true });

    act(() => vi.advanceTimersByTime(5000));
    expect(result.current.secondsLeft).toBe(0);
  });

  it('is already expired for a target in the past', () => {
    const { result } = renderHook(() => useCountdown(new Date(NOW.getTime() - 1000)));
    expect(result.current).toEqual({ secondsLeft: 0, isExpired: true });
  });

  it('follows the clock rather than counting ticks, so a throttled tab stays accurate', () => {
    const target = new Date(NOW.getTime() + 30000);
    const { result } = renderHook(() => useCountdown(target));
    act(() => {
      vi.setSystemTime(NOW.getTime() + 20000);
      vi.advanceTimersByTime(1000);
    });
    expect(result.current.secondsLeft).toBeLessThanOrEqual(10);
  });

  it('tolerates a fresh Date object for the same instant on every render', () => {
    const { result, rerender } = renderHook(() => useCountdown(new Date(NOW.getTime() + 2000)));
    rerender();
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current.secondsLeft).toBe(1);
  });
});
