export async function apiFetch(path, options = {}) {
  const token = localStorage.getItem('token');
  const headers = { ...options.headers };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return fetch(`/api${path}`, { ...options, headers });
}
