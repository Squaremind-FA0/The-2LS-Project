import '@testing-library/jest-dom/vitest';

// Node 25+ defines its own global localStorage, which is undefined unless
// --localstorage-file is set and shadows jsdom's. Restore jsdom's real Storage
// so the suite behaves the same on every Node version.
if (!globalThis.localStorage && globalThis.jsdom) {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: globalThis.jsdom.window.localStorage,
  });
}
