import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useRef } from 'react';
import { describe, expect, it } from 'vitest';

import { useFocusTrap } from './useFocusTrap.js';

function Trapped({ disabled = false }) {
  const ref = useRef(null);
  useFocusTrap(ref);
  return (
    <div ref={ref} tabIndex={-1}>
      <button>first</button>
      <button disabled={disabled}>middle</button>
      <button>last</button>
    </div>
  );
}

const button = (name) => screen.getByRole('button', { name });

describe('useFocusTrap', () => {
  it('useFocusTrap keeps Tab/Shift+Tab cycling inside the container', async () => {
    const user = userEvent.setup();
    render(
      <>
        <button>outside before</button>
        <Trapped />
        <button>outside after</button>
      </>,
    );
    expect(button('first')).toHaveFocus();
    await user.tab();
    expect(button('middle')).toHaveFocus();
    await user.tab();
    expect(button('last')).toHaveFocus();
    await user.tab();
    expect(button('first')).toHaveFocus();
    await user.tab({ shift: true });
    expect(button('last')).toHaveFocus();
  });

  it('skips disabled controls', async () => {
    const user = userEvent.setup();
    render(<Trapped disabled />);
    await user.tab();
    expect(button('last')).toHaveFocus();
  });

  it('pulls focus back in if it has escaped the container', async () => {
    const user = userEvent.setup();
    render(
      <>
        <button>outside</button>
        <Trapped />
      </>,
    );
    button('outside').focus();
    await user.tab();
    expect(button('first')).toHaveFocus();
  });

  it('returns focus to where it was when the trap ends', () => {
    const { rerender } = render(
      <>
        <button>opener</button>
      </>,
    );
    button('opener').focus();
    rerender(
      <>
        <button>opener</button>
        <Trapped />
      </>,
    );
    expect(button('first')).toHaveFocus();
    rerender(
      <>
        <button>opener</button>
      </>,
    );
    expect(button('opener')).toHaveFocus();
  });
});
