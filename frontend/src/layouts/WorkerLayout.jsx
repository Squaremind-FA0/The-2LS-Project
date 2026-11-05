import { Outlet } from 'react-router-dom';

import { Nav } from '../components/Nav.jsx';
import { ShareLocationToggle } from '../location/ShareLocationToggle.jsx';
import { OfferProvider } from '../offers/OfferProvider.jsx';

export function WorkerLayout() {
  return (
    <>
      <header className="app-header">
        <Nav />
        <ShareLocationToggle />
      </header>
      <OfferProvider>
        <main className="page">
          <Outlet />
        </main>
      </OfferProvider>
    </>
  );
}
