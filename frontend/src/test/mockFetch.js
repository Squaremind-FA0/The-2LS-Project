import { vi } from 'vitest';

/**
 * Stub global fetch with a route table. Keys are `METHOD /path`, matched
 * against the request path including its query string. A value is a response
 * spec `{ status, body }`, or a function `(request) => spec`, or an array of
 * those consumed one call at a time (the last entry repeats).
 *
 * An unrouted request rejects the test's fetch instead of returning a default,
 * so a page calling an endpoint nobody planned for fails loudly.
 */
export function mockFetch(routes = {}) {
  const calls = new Map();
  const fetchMock = vi.fn(async (url, options = {}) => {
    const method = options.method ?? 'GET';
    const key = `${method} ${url.replace(/^\/api/, '')}`;
    const route = routes[key];
    if (route === undefined) {
      throw new Error(`mockFetch: no route for ${key}`);
    }
    const seen = calls.get(key) ?? 0;
    calls.set(key, seen + 1);
    let spec = Array.isArray(route) ? route[Math.min(seen, route.length - 1)] : route;
    if (typeof spec === 'function') {
      spec = spec({ url, options, body: options.body ? JSON.parse(options.body) : undefined });
    }
    const { status = 200, body } = spec;
    const text = typeof body === 'string' ? body : JSON.stringify(body ?? null);
    return new Response(status === 204 ? null : text, { status });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** The parsed JSON bodies sent to `METHOD /path` so far. */
export function sentBodies(fetchMock, key) {
  return fetchMock.mock.calls
    .filter(([url, options = {}]) => `${options.method ?? 'GET'} ${url.replace(/^\/api/, '')}` === key)
    .map(([, options]) => (options.body ? JSON.parse(options.body) : undefined));
}
