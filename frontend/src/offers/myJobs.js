// There is no worker-readable job list on the backend yet (PRP G6, known backend
// gap 1), so "my jobs" is what this browser saw accepted. Keyed per user so a
// shared device never shows one worker another's jobs.
const storageKey = (userId) => `myJobs:${userId}`;

export function loadJobs(userId) {
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey(userId)));
    return Array.isArray(stored) ? stored : [];
  } catch {
    return [];
  }
}

export function saveJobs(userId, jobs) {
  localStorage.setItem(storageKey(userId), JSON.stringify(jobs));
}
