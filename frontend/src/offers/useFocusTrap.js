import { useEffect } from 'react';

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Keeps Tab and Shift+Tab inside `containerRef` while mounted, focusing the
 * first control on entry and returning focus to where it was on exit.
 *
 * Listens on the document, not the container: if the focused control becomes
 * disabled the browser drops focus to <body>, and a listener on the container
 * would never see the next Tab.
 */
export function useFocusTrap(containerRef) {
  useEffect(() => {
    const container = containerRef.current;
    const previouslyFocused = document.activeElement;
    const focusable = () => [...container.querySelectorAll(FOCUSABLE)];

    (focusable()[0] ?? container).focus();

    function handleKeyDown(event) {
      if (event.key !== 'Tab') {
        return;
      }
      const items = focusable();
      if (items.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (!container.contains(active) || active === container) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      if (previouslyFocused?.isConnected) {
        previouslyFocused.focus();
      }
    };
  }, [containerRef]);
}
