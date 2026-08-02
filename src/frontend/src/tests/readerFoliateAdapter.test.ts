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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

interface TransformDetail {
  data: unknown;
  name: string;
  type: unknown;
}

const dispatchTransform = (target: EventTarget, detail: TransformDetail) => {
  const event = new Event('data');
  Object.defineProperty(event, 'detail', { value: detail });
  target.dispatchEvent(event);
};

const mockView = (
  transformTarget: EventTarget,
  options: { initialRender?: () => Promise<void>; layout?: string } = {},
) =>
  Object.assign(new EventTarget(), {
    book: {
      rendition: { layout: options.layout ?? 'reflowable' },
      sections: [{ id: 'page-1.xhtml' }, { id: 'page-2.xhtml' }, { id: 'page-3.xhtml' }],
      toc: [],
      transformTarget,
    },
    className: '',
    renderer: {
      next: vi.fn(options.initialRender ?? (async () => undefined)),
    },
    open: vi.fn(async (_source: File): Promise<void> => undefined),
    close: vi.fn(),
    getCFI: vi.fn((index: number) => `epubcfi(/6/${(index + 1) * 2})`),
    goTo: vi.fn(async () => undefined as unknown),
    next: vi.fn(async () => undefined),
    prev: vi.fn(async () => undefined),
    remove: vi.fn(),
  });

const stubBookFetchAndView = (view: ReturnType<typeof mockView>) => {
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
};

const mockContainer = () => {
  const replaceChildren = vi.fn();
  // The adapter only uses replaceChildren on this test container.
  // oxlint-disable-next-line typescript/no-unsafe-type-assertion
  return { container: { replaceChildren } as unknown as HTMLElement, replaceChildren };
};

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

  it('uses fit-to-page CSS only when fixed-layout author CSS fails', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const transformTarget = new EventTarget();
    const view = mockView(transformTarget, { layout: 'pre-paginated' });
    stubBookFetchAndView(view);
    const adapter = await createFoliateAdapter('fixed');
    await adapter.open(mockContainer().container, file);
    const failedCss: TransformDetail = {
      data: Promise.reject(new Error('broken stylesheet')),
      name: 'styles/cover.css',
      type: Promise.resolve('text/css; charset=utf-8'),
    };

    dispatchTransform(transformTarget, failedCss);

    await expect(failedCss.data).resolves.toEqual(expect.stringContaining('object-fit: contain'));
    await expect(failedCss.data).resolves.toEqual(expect.stringContaining('max-width: 100%'));
    expect(console.warn).toHaveBeenCalledWith(
      expect.stringContaining('styles/cover.css (text/css, file 36)'),
      expect.any(Error),
    );
  });

  it('leaves valid author CSS unchanged', async () => {
    const transformTarget = new EventTarget();
    const view = mockView(transformTarget, { layout: 'pre-paginated' });
    stubBookFetchAndView(view);
    const adapter = await createFoliateAdapter('fixed');
    await adapter.open(mockContainer().container, file);
    const authorCss = 'img { width: 535px; height: 800px; }';
    const validCss: TransformDetail = {
      data: Promise.resolve(authorCss),
      name: 'styles/cover.css',
      type: 'text/css',
    };

    dispatchTransform(transformTarget, validCss);

    await expect(validCss.data).resolves.toBe(authorCss);
  });

  it('degrades optional media failures but rejects required document failures', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const transformTarget = new EventTarget();
    const view = mockView(transformTarget, { layout: 'pre-paginated' });
    stubBookFetchAndView(view);
    const adapter = await createFoliateAdapter('fixed');
    await adapter.open(mockContainer().container, file);
    const image: TransformDetail = {
      data: Promise.reject(new Error('missing image')),
      name: 'images/optional.jpg',
      type: 'image/jpeg',
    };
    const chapter: TransformDetail = {
      data: Promise.reject(new Error('missing chapter')),
      name: 'text/chapter-12.xhtml',
      type: 'application/xhtml+xml',
    };

    dispatchTransform(transformTarget, image);
    dispatchTransform(transformTarget, chapter);

    await expect(image.data).resolves.toBe('');
    await expect(chapter.data).rejects.toThrow(
      'Required EPUB resource text/chapter-12.xhtml (application/xhtml+xml, file 36)',
    );

    const svgPage: TransformDetail = {
      data: Promise.reject(new Error('missing SVG page')),
      name: 'pages/page-13.svg',
      type: 'image/svg+xml',
    };
    view.book.sections.push({ id: 'pages/page-13.svg' });
    dispatchTransform(transformTarget, svgPage);
    await expect(svgPage.data).rejects.toThrow(
      'Required EPUB resource pages/page-13.svg (image/svg+xml, file 36)',
    );

    const decorativeSvg: TransformDetail = {
      data: Promise.reject(new Error('missing icon')),
      name: 'images/icon.svg',
      type: 'image/svg+xml',
    };
    dispatchTransform(transformTarget, decorativeSvg);
    await expect(decorativeSvg.data).resolves.toBe('');
  });

  it('lets Foliate handle malformed percentage locators', async () => {
    const transformTarget = new EventTarget();
    const view = mockView(transformTarget);
    stubBookFetchAndView(view);
    const adapter = await createFoliateAdapter('reflowable');
    await adapter.open(mockContainer().container, file);

    await adapter.goTo('fraction:not-a-number');

    expect(view.goTo).toHaveBeenCalledWith('fraction:not-a-number');
  });

  it('bridges a resource error swallowed by Foliate back to navigation', async () => {
    const transformTarget = new EventTarget();
    const view = mockView(transformTarget);
    stubBookFetchAndView(view);
    const adapter = await createFoliateAdapter('reflowable');
    await adapter.open(mockContainer().container, file);
    view.next.mockImplementationOnce(async () => {
      const chapter: TransformDetail = {
        data: Promise.reject(new Error('missing chapter')),
        name: 'text/chapter-12.xhtml',
        type: 'application/xhtml+xml',
      };
      dispatchTransform(transformTarget, chapter);
      await Promise.resolve(chapter.data).catch(() => undefined);
    });

    await expect(adapter.next()).rejects.toThrow(
      'Required EPUB resource text/chapter-12.xhtml (application/xhtml+xml, file 36)',
    );
  });

  it('reports required resource errors from Foliate-internal navigation', async () => {
    const transformTarget = new EventTarget();
    const view = mockView(transformTarget);
    stubBookFetchAndView(view);
    const adapter = await createFoliateAdapter('reflowable');
    await adapter.open(mockContainer().container, file);
    const onError = vi.fn();
    const unsubscribe = adapter.subscribeErrors?.(onError);
    const chapter: TransformDetail = {
      data: Promise.reject(new Error('missing swiped chapter')),
      name: 'text/chapter-13.xhtml',
      type: 'application/xhtml+xml',
    };

    dispatchTransform(transformTarget, chapter);
    await Promise.resolve(chapter.data).catch(() => undefined);

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({
        message:
          'Required EPUB resource text/chapter-13.xhtml (application/xhtml+xml, file 36) could not be loaded.',
      }),
    );
    unsubscribe?.();
  });

  it('publishes a fixed-layout target when Foliate changes sides without relocating', async () => {
    const transformTarget = new EventTarget();
    const view = mockView(transformTarget, { layout: 'pre-paginated' });
    view.goTo.mockResolvedValueOnce({ index: 2 });
    stubBookFetchAndView(view);
    const adapter = await createFoliateAdapter('fixed');
    await adapter.open(mockContainer().container, file);
    const onLocator = vi.fn();
    adapter.subscribe(onLocator);

    await adapter.goTo('page-3.xhtml#end-sentinel');

    expect(onLocator).toHaveBeenCalledWith({
      value: 'epubcfi(/6/6)',
      progression: 1,
      currentPage: 3,
      totalPages: 3,
    });
    expect(adapter.getLocator()).toEqual({
      value: 'epubcfi(/6/6)',
      progression: 1,
      currentPage: 3,
      totalPages: 3,
    });
  });

  it('rejects opening when the first section cannot be rendered', async () => {
    const transformTarget = new EventTarget();
    const view = mockView(transformTarget, {
      initialRender: async () => {
        throw new Error('renderer failed');
      },
    });
    const removeTransformListener = vi.spyOn(transformTarget, 'removeEventListener');
    const removeRelocationListener = vi.spyOn(view, 'removeEventListener');
    stubBookFetchAndView(view);
    const adapter = await createFoliateAdapter('reflowable');

    await expect(adapter.open(mockContainer().container, file)).rejects.toThrow(
      'The first EPUB section could not be rendered.',
    );
    expect(view.close).toHaveBeenCalledOnce();
    expect(view.remove).toHaveBeenCalledOnce();
    expect(removeTransformListener).toHaveBeenCalledWith('data', expect.any(Function));
    expect(removeRelocationListener).toHaveBeenCalledWith('relocate', expect.any(Function));
  });

  it('removes resource and relocation listeners when closed', async () => {
    const transformTarget = new EventTarget();
    const removeTransformListener = vi.spyOn(transformTarget, 'removeEventListener');
    const view = mockView(transformTarget);
    const removeRelocationListener = vi.spyOn(view, 'removeEventListener');
    stubBookFetchAndView(view);
    const adapter = await createFoliateAdapter('reflowable');
    await adapter.open(mockContainer().container, file);

    adapter.close();

    expect(removeTransformListener).toHaveBeenCalledWith('data', expect.any(Function));
    expect(removeRelocationListener).toHaveBeenCalledWith('relocate', expect.any(Function));
  });
});
