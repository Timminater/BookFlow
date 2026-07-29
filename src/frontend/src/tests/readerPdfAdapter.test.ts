import { afterEach, describe, expect, it, vi } from 'vitest';

const { getDocument } = vi.hoisted(() => ({ getDocument: vi.fn() }));

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: {},
  getDocument,
}));
vi.mock('pdfjs-dist/build/pdf.worker.mjs?url', () => ({ default: '/pdf.worker.mjs' }));

import { createPdfAdapter } from '../reader/adapters/pdfAdapter';
import type { ReaderFile } from '../reader/types';

const file: ReaderFile = {
  bookId: 7,
  fileId: 36,
  title: 'Document',
  format: 'PDF',
  contentUrl: '/document.pdf',
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('PDF reader position', () => {
  it('publishes exact current and total pages on every page change', async () => {
    const render = vi.fn(() => ({ promise: Promise.resolve(), cancel: vi.fn() }));
    const documentProxy = {
      numPages: 200,
      getOutline: vi.fn(async () => []),
      getPage: vi.fn(async () => ({
        getViewport: ({ scale }: { scale: number }) => ({
          width: 600 * scale,
          height: 800 * scale,
        }),
        render,
      })),
      destroy: vi.fn(async () => undefined),
    };
    getDocument.mockReturnValue({ promise: Promise.resolve(documentProxy) });
    const canvas = {
      className: '',
      width: 0,
      height: 0,
      style: { width: '', height: '' },
      getContext: vi.fn(() => ({})),
      remove: vi.fn(),
    };
    vi.stubGlobal('document', { createElement: vi.fn(() => canvas) });
    vi.stubGlobal('window', { devicePixelRatio: 1 });
    const container = {
      clientWidth: 1024,
      clientHeight: 768,
      replaceChildren: vi.fn(),
    };
    const adapter = await createPdfAdapter();

    // The adapter only needs the measured dimensions and replaceChildren in this test.
    // oxlint-disable-next-line typescript/no-unsafe-type-assertion
    await adapter.open(container as unknown as HTMLElement, file);
    const updates: unknown[] = [];
    adapter.subscribe((locator) => updates.push(locator));
    await adapter.goTo('page:36');

    expect(adapter.getLocator()).toEqual({
      value: 'page:36',
      progression: 35 / 199,
      currentPage: 36,
      totalPages: 200,
    });
    expect(updates).toContainEqual(adapter.getLocator());

    await adapter.goTo('page:999');
    expect(adapter.getLocator()).toMatchObject({
      value: 'page:200',
      progression: 1,
      currentPage: 200,
      totalPages: 200,
    });
  });
});
