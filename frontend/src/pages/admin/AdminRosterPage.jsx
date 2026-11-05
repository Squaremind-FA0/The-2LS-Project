// GET /api/shifts is worker-only, so there is nothing to fetch here without a
// 403 (PRP G6, known backend gap 3).
export function AdminRosterPage() {
  return (
    <>
      <h1>Roster</h1>
      <p>
        A company-wide roster is not available yet. Workers manage their own shifts, and the backend
        has no way to read them across the company until a roster endpoint is added.
      </p>
    </>
  );
}
