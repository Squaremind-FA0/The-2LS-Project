import { useShareLocation } from './useShareLocation.js';

export function ShareLocationToggle() {
  const { sharing, error, toggle } = useShareLocation();
  return (
    <div className="share-location">
      <label>
        <input type="checkbox" role="switch" checked={sharing} onChange={toggle} />
        Share my location
      </label>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
