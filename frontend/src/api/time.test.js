import { describe, expect, it } from 'vitest';

import { parseUtc } from './time.js';

describe('parseUtc', () => {
  it('parseUtc reads a naive backend timestamp as UTC, not local time', () => {
    const parsed = parseUtc('2026-11-05T10:00:30');
    expect(parsed).toEqual(new Date('2026-11-05T10:00:30Z'));
    expect(parsed.toISOString()).toBe('2026-11-05T10:00:30.000Z');
  });

  it('keeps fractional seconds on a naive timestamp', () => {
    expect(parseUtc('2026-11-05T10:00:30.250000').toISOString()).toBe('2026-11-05T10:00:30.250Z');
  });

  it('respects an explicit offset instead of appending Z', () => {
    expect(parseUtc('2026-11-05T10:00:30Z').toISOString()).toBe('2026-11-05T10:00:30.000Z');
    expect(parseUtc('2026-11-05T10:00:30+02:00').toISOString()).toBe('2026-11-05T08:00:30.000Z');
  });

  it('does not mistake the day of a date-only value for an offset', () => {
    expect(parseUtc('2026-11-05').toISOString()).toBe('2026-11-05T00:00:00.000Z');
  });

  it('returns null for a missing value', () => {
    expect(parseUtc(null)).toBeNull();
  });
});
