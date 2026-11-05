export const UNAUTHORIZED_EVENT = 'api:unauthorized';

const LOGIN_PATH = '/auth/login';

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export async function apiFetch(path, options = {}) {
  const token = localStorage.getItem('token');
  const headers = { ...options.headers };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const response = await fetch(`/api${path}`, { ...options, headers });
  // The server is the sole authority on the session: a rejected token ends it,
  // wherever in the app the request came from. A failed login is not that.
  if (response.status === 401 && token && path !== LOGIN_PATH) {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
  return response;
}

// HTTPException details are strings; request validation (422) details are a
// list of {loc, msg}, which would otherwise render as "[object Object]".
function describeDetail(detail, status) {
  if (typeof detail === 'string' && detail) {
    return detail;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map(({ loc = [], msg = 'Invalid value' }) => {
        const field = loc.filter((part) => part !== 'body').join('.');
        return field ? `${field}: ${msg}` : msg;
      })
      .join('; ');
  }
  return `Request failed (${status})`;
}

export async function apiRequest(path, { json, ...options } = {}) {
  const init = { ...options };
  if (json !== undefined) {
    init.body = JSON.stringify(json);
    init.headers = { 'Content-Type': 'application/json', ...options.headers };
  }
  const response = await apiFetch(path, init);
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    throw new ApiError(response.status, describeDetail(body?.detail, response.status));
  }
  return body;
}

async function apiBlob(path) {
  const response = await apiFetch(path);
  if (!response.ok) {
    const detail = await response.json().then((body) => body?.detail, () => undefined);
    throw new ApiError(response.status, describeDetail(detail, response.status));
  }
  return response.blob();
}

function withQuery(path, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      query.set(key, value);
    }
  }
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}

const id = (value) => encodeURIComponent(value);

export const auth = {
  login: (email, password) =>
    apiRequest(LOGIN_PATH, { method: 'POST', json: { email, password } }),
};

export const me = {
  get: () => apiRequest('/me'),
};

export const workers = {
  create: (payload) => apiRequest('/workers', { method: 'POST', json: payload }),
  list: () => apiRequest('/workers'),
  get: (workerId) => apiRequest(`/workers/${id(workerId)}`),
};

export const jobs = {
  create: (payload) => apiRequest('/jobs', { method: 'POST', json: payload }),
  list: (params) => apiRequest(withQuery('/jobs', params)),
  transition: (jobId, status) =>
    apiRequest(`/jobs/${id(jobId)}`, { method: 'PATCH', json: { status } }),
  clockIn: (jobId) => apiRequest(`/jobs/${id(jobId)}/clock-in`, { method: 'POST' }),
  clockOut: (jobId) => apiRequest(`/jobs/${id(jobId)}/clock-out`, { method: 'POST' }),
};

export const offers = {
  pending: (params) => apiRequest(withQuery('/offers/pending', params)),
  accept: (offerId) => apiRequest(`/offers/${id(offerId)}/accept`, { method: 'POST' }),
  decline: (offerId) => apiRequest(`/offers/${id(offerId)}/decline`, { method: 'POST' }),
};

export const location = {
  update: (lat, lng) => apiRequest('/workers/me/location', { method: 'PUT', json: { lat, lng } }),
};

export const shifts = {
  create: (payload) => apiRequest('/shifts', { method: 'POST', json: payload }),
  list: (params) => apiRequest(withQuery('/shifts', params)),
};

export const payruns = {
  list: (params) => apiRequest(withQuery('/payruns', params)),
  create: (payload) => apiRequest('/payruns', { method: 'POST', json: payload }),
  transition: (payrunId, status) =>
    apiRequest(`/payruns/${id(payrunId)}`, { method: 'PATCH', json: { status } }),
  paylines: (payrunId) => apiRequest(`/payruns/${id(payrunId)}/paylines`),
  exportUrl: (payrunId) => `/api/payruns/${id(payrunId)}/export?format=csv`,
  exportCsv: (payrunId) => apiBlob(`/payruns/${id(payrunId)}/export?format=csv`),
};
