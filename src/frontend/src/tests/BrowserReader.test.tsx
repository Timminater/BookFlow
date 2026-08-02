// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LanguageProvider } from '../contexts/LanguageContext';
import { BrowserReader } from '../reader/components/BrowserReader';
import type { ReaderAdapter, ReaderFile } from '../reader/types';

const file: ReaderFile = {
  bookId: 7,
  fileId: 36,
  title: 'Fixed book',
  format: 'EPUB',
  contentUrl: '/book.epub',
};

const mockAdapter = () => {
  const open = vi.fn(async () => undefined);
  const next = vi.fn(async () => undefined);
  const goTo = vi.fn(async () => undefined);
  let errorListener: ((error: Error) => void) | null = null;
  const adapter: ReaderAdapter = {
    kind: 'fixed',
    open,
    close: vi.fn(),
    next,
    previous: vi.fn(async () => undefined),
    goTo,
    setPreferences: vi.fn(async () => undefined),
    getLocator: vi.fn(() => null),
    getToc: vi.fn(() => [{ id: 'chapter-2', label: 'Chapter 2', href: 'chapter-2.xhtml' }]),
    subscribe: vi.fn(() => () => undefined),
    subscribeErrors: vi.fn((listener: (error: Error) => void) => {
      errorListener = listener;
      return () => {
        errorListener = null;
      };
    }),
  };
  return { adapter, emitError: (error: Error) => errorListener?.(error), goTo, next, open };
};

const renderReader = (adapter: ReaderAdapter) => {
  render(
    <LanguageProvider>
      <BrowserReader
        file={file}
        adapterFactory={vi.fn(async () => adapter)}
        onBack={vi.fn()}
        onProgressChange={vi.fn()}
        onDownload={vi.fn(async () => undefined)}
      />
    </LanguageProvider>,
  );
};

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe('BrowserReader action failures', () => {
  it('shows a reader error when next-page navigation fails', async () => {
    const { adapter, next, open } = mockAdapter();
    next.mockRejectedValueOnce(new Error('Next section is unavailable'));
    renderReader(adapter);
    await waitFor(() => expect(open).toHaveBeenCalledOnce());

    fireEvent.keyDown(document, { key: 'ArrowRight' });

    expect(await screen.findByText('Next section is unavailable')).toBeTruthy();
  });

  it('shows a reader error when table-of-contents navigation fails', async () => {
    const { adapter, goTo, open } = mockAdapter();
    goTo.mockRejectedValueOnce(new Error('Chapter could not be displayed'));
    renderReader(adapter);
    await waitFor(() => expect(open).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole('button', { name: 'Contents' }));
    fireEvent.click(screen.getByRole('button', { name: 'Chapter 2' }));

    expect(await screen.findByText('Chapter could not be displayed')).toBeTruthy();
  });

  it('shows a reader error from Foliate-internal navigation', async () => {
    const { adapter, emitError, open } = mockAdapter();
    renderReader(adapter);
    await waitFor(() => expect(open).toHaveBeenCalledOnce());

    emitError(new Error('Swiped section could not be displayed'));

    expect(await screen.findByText('Swiped section could not be displayed')).toBeTruthy();
  });
});
