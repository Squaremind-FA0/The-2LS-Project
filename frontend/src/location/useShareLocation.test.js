import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { mockFetch, sentBodies } from '../test/mockFetch.js';
import { useShareLocation } from './useShareLocation.js';

let geolocation;
const position = (latitude, longitude) => ({ coords: { latitude, longitude } });

beforeEach(() => {
  geolocation = { watchPosition: vi.fn(() => 42), clearWatch: vi.fn() };
  vi.stubGlobal('navigator', { ...navigator, geolocation });
});

afterEach(() => {
  cleanup(); // unmount, and so clearWatch, while the geolocation stub still exists
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('useShareLocation', () => {
  it('useShareLocation toggling on calls watchPosition and PUT /api/workers/me/location; toggling off clears the watch', async () => {
    const fetchMock = mockFetch({ 'PUT /workers/me/location': { body: { worker_id: 1 } } });
    const { result } = renderHook(() => useShareLocation());
    expect(result.current.sharing).toBe(false);
    expect(geolocation.watchPosition).not.toHaveBeenCalled();

    act(() => result.current.toggle());
    expect(result.current.sharing).toBe(true);
    expect(geolocation.watchPosition).toHaveBeenCalledTimes(1);

    const onPosition = geolocation.watchPosition.mock.calls[0][0];
    await act(async () => onPosition(position(-33.8, 151.2)));
    await act(async () => onPosition(position(-33.9, 151.3)));
    await waitFor(() =>
      expect(sentBodies(fetchMock, 'PUT /workers/me/location')).toEqual([
        { lat: -33.8, lng: 151.2 },
        { lat: -33.9, lng: 151.3 },
      ]),
    );

    act(() => result.current.toggle());
    expect(result.current.sharing).toBe(false);
    expect(geolocation.clearWatch).toHaveBeenCalledWith(42);
  });

  it('stops watching on unmount', () => {
    const { result, unmount } = renderHook(() => useShareLocation());
    act(() => result.current.toggle());
    unmount();
    expect(geolocation.clearWatch).toHaveBeenCalledWith(42);
  });

  it('turns itself off and explains when permission is denied', () => {
    const { result } = renderHook(() => useShareLocation());
    act(() => result.current.toggle());
    const onError = geolocation.watchPosition.mock.calls[0][1];
    act(() => onError({ code: 1, PERMISSION_DENIED: 1, message: 'denied' }));
    expect(result.current.sharing).toBe(false);
    expect(geolocation.clearWatch).toHaveBeenCalledWith(42);
    expect(result.current.error).toMatch(/permission/i);
  });

  it('keeps sharing through a transient position error', () => {
    const { result } = renderHook(() => useShareLocation());
    act(() => result.current.toggle());
    const onError = geolocation.watchPosition.mock.calls[0][1];
    act(() => onError({ code: 2, PERMISSION_DENIED: 1, message: 'unavailable' }));
    expect(result.current.sharing).toBe(true);
    expect(result.current.error).toBeTruthy();
  });

  it('surfaces a failed location update and keeps watching', async () => {
    mockFetch({ 'PUT /workers/me/location': { status: 500, body: { detail: 'Boom' } } });
    const { result } = renderHook(() => useShareLocation());
    act(() => result.current.toggle());
    await act(async () => geolocation.watchPosition.mock.calls[0][0](position(1, 2)));
    await waitFor(() => expect(result.current.error).toBe('Boom'));
    expect(result.current.sharing).toBe(true);
  });

  it('reports a browser with no geolocation instead of throwing', () => {
    vi.stubGlobal('navigator', {});
    const { result } = renderHook(() => useShareLocation());
    act(() => result.current.toggle());
    expect(result.current.sharing).toBe(false);
    expect(result.current.error).toMatch(/not supported/i);
  });
});
