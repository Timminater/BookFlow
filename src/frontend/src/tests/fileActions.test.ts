import { afterEach, describe, expect, it, vi } from 'vitest';

import { shareBookFile } from '../reader/fileActions';

const originalShare = Object.getOwnPropertyDescriptor(navigator, 'share');
const originalCanShare = Object.getOwnPropertyDescriptor(navigator, 'canShare');
const file = new File(['book'], 'book.epub', { type: 'application/epub+zip' });

afterEach(() => {
  if (originalShare) Object.defineProperty(navigator, 'share', originalShare);
  else Reflect.deleteProperty(navigator, 'share');
  if (originalCanShare) Object.defineProperty(navigator, 'canShare', originalCanShare);
  else Reflect.deleteProperty(navigator, 'canShare');
});

describe('shareBookFile', () => {
  it('starts sharing synchronously with an already prepared File', async () => {
    let resolveShare: (() => void) | undefined;
    const share = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveShare = resolve;
        }),
    );
    Object.defineProperty(navigator, 'share', { configurable: true, value: share });
    Object.defineProperty(navigator, 'canShare', {
      configurable: true,
      value: vi.fn(() => true),
    });

    const sharePromise = shareBookFile(file, 'Prepared book');

    // This assertion runs before the native share promise settles. It protects
    // the transient user-activation path from a preceding await/fetch.
    expect(share).toHaveBeenCalledWith({ files: [file], title: 'Prepared book' });
    resolveShare?.();
    await sharePromise;
  });

  it('does not invoke share when the browser rejects file sharing', async () => {
    const share = vi.fn();
    Object.defineProperty(navigator, 'share', { configurable: true, value: share });
    Object.defineProperty(navigator, 'canShare', {
      configurable: true,
      value: vi.fn(() => false),
    });

    expect(() => shareBookFile(file, 'Prepared book')).toThrow('kan dit boekformaat niet');
    expect(share).not.toHaveBeenCalled();
  });

  it('reports browsers without Web Share support', async () => {
    Object.defineProperty(navigator, 'share', { configurable: true, value: undefined });

    expect(() => shareBookFile(file, 'Prepared book')).toThrow('niet ondersteund');
  });

  it('preserves an AbortError so callers can silently handle cancellation', async () => {
    const aborted = new DOMException('cancelled', 'AbortError');
    Object.defineProperty(navigator, 'share', {
      configurable: true,
      value: vi.fn(() => Promise.reject(aborted)),
    });

    await expect(shareBookFile(file, 'Prepared book')).rejects.toBe(aborted);
  });
});
