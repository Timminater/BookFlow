import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('foliate-js/view.js', () => ({}));

import { createFoliateAdapter } from '../reader/adapters/foliateAdapter';
import type { ReaderFile } from '../reader/types';

const file: ReaderFile = {
  bookId: 7,
  fileId: 36,
  title: 'Book',
  format: 'EPUB',
  contentUrl: '/book.epub',
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Foliate adapter readiness', () => {
  it('does not finish opening before the initial renderer navigation completes', async () => {
    let finishInitialRender: (() => void) | undefined;
    const initialRender = new Promise<void>((resolve) => {
      finishInitialRender = resolve;
    });
    const transformTarget = new EventTarget();
    const view = Object.assign(new EventTarget(), {
      book: { toc: [], transformTarget },
      className: '',
      renderer: {
        next: vi.fn(() => initialRender),
      },
      open: vi.fn(async (_source: File): Promise<void> => undefined),
      close: vi.fn(),
      remove: vi.fn(),
    });
    vi.stubGlobal('document', {
      createElement: vi.fn(() => view),
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(new Blob(['epub'], { type: 'application/epub+zip' }), {
            status: 200,
            headers: { 'Content-Type': 'application/epub+zip' },
          }),
      ),
    );
    const replaceChildren = vi.fn();
    const adapter = await createFoliateAdapter('reflowable');
    let opened = false;

    const opening = adapter
      // The adapter only uses replaceChildren on this test container.
      // oxlint-disable-next-line typescript/no-unsafe-type-assertion
      .open({ replaceChildren } as unknown as HTMLElement, file)
      .then(() => {
        opened = true;
      });

    await vi.waitFor(() => expect(view.renderer.next).toHaveBeenCalledOnce());
    expect(opened).toBe(false);
    expect(replaceChildren).toHaveBeenCalledWith(view);

    finishInitialRender?.();
    await opening;

    expect(opened).toBe(true);
  });

  it('keeps reflowable EPUB locations percentage-only after reader setting changes', async () => {
    const transformTarget = new EventTarget();
    const renderer = Object.assign(new EventTarget(), {
      next: vi.fn(async () => undefined),
      setAttribute: vi.fn(),
      setStyles: vi.fn(),
    });
    const view = Object.assign(new EventTarget(), {
      book: { toc: [], transformTarget },
      className: '',
      renderer,
      open: vi.fn(async (_source: File): Promise<void> => undefined),
      close: vi.fn(),
      remove: vi.fn(),
    });
    vi.stubGlobal('document', {
      createElement: vi.fn(() => view),
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(new Blob(['epub'], { type: 'application/epub+zip' }), {
            status: 200,
            headers: { 'Content-Type': 'application/epub+zip' },
          }),
      ),
    );
    const adapter = await createFoliateAdapter('reflowable');
    // The adapter only uses replaceChildren on this test container.
    // oxlint-disable-next-line typescript/no-unsafe-type-assertion
    await adapter.open({ replaceChildren: vi.fn() } as unknown as HTMLElement, file);

    const relocate = new Event('relocate');
    Object.defineProperty(relocate, 'detail', {
      value: { cfi: 'epubcfi(/6/4)', fraction: 0.36 },
    });
    view.dispatchEvent(relocate);
    await adapter.setPreferences({
      theme: 'sepia',
      fontFamily: 'serif',
      fontSize: 140,
      lineHeight: 1.8,
      flow: 'paginated',
      spread: 'single',
    });

    expect(adapter.getLocator()).toEqual({
      value: 'epubcfi(/6/4)',
      progression: 0.36,
    });
    expect(adapter.getLocator()).not.toHaveProperty('currentPage');
    expect(adapter.getLocator()).not.toHaveProperty('totalPages');
  });
});
