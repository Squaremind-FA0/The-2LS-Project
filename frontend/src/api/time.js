// A time component ending in Z or a numeric offset. Anchored on the T so the
// day of a date-only value ("2026-11-05") is not mistaken for a "-05" offset.
const HAS_OFFSET = /T.*(Z|[+-]\d{2}(:?\d{2})?)$/i;

/**
 * The only sanctioned way to turn an API timestamp into a Date. The backend
 * serialises naive UTC with no suffix, which `new Date()` would read as local
 * time and silently shift every countdown by the browser's UTC offset.
 */
export function parseUtc(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const text = String(value);
  const isNaiveDateTime = text.includes('T') && !HAS_OFFSET.test(text);
  return new Date(isNaiveDateTime ? `${text}Z` : text);
}

/** A local-time display string for an API timestamp. */
export function formatUtc(value) {
  const parsed = parseUtc(value);
  return parsed ? parsed.toLocaleString() : '';
}

/**
 * A `<input type="datetime-local">` value is the user's wall-clock time with no
 * offset, and the backend treats an offset-less datetime as UTC, so it has to
 * be converted before it is sent.
 */
export function localInputToUtcIso(value) {
  return new Date(value).toISOString();
}
