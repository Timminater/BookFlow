// @vitest-environment jsdom
import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useDependencyEffect } from '../hooks/useMountEffect';

describe('useDependencyEffect', () => {
  it('runs the latest callback on dependency changes and cleans up', () => {
    const events: string[] = [];
    const { rerender, unmount } = renderHook(
      ({ dependency, label }) => {
        useDependencyEffect(() => {
          events.push(`start:${label}`);
          return () => {
            events.push(`stop:${label}`);
          };
        }, [dependency]);
      },
      { initialProps: { dependency: 1, label: 'initial' } },
    );
    rerender({ dependency: 1, label: 'updated' });
    expect(events).toEqual(['start:initial']);
    rerender({ dependency: 2, label: 'new' });
    expect(events).toEqual(['start:initial', 'stop:initial', 'start:new']);
    unmount();
    expect(events).toEqual(['start:initial', 'stop:initial', 'start:new', 'stop:new']);
  });
});
