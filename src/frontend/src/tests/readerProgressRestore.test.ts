import { afterEach, describe, expect, it, vi } from 'vitest';

import { getReaderProgress, readerLocatorForOpen, saveReaderProgress } from '../reader/readerApi';
import { loadReaderLocator, saveReaderLocator } from '../reader/readerLocatorStore';
import { v1Api } from '../services/v1Api';
import type { V1ReadingProgress } from '../types/v1';

const serverProgress = (fileId: number): V1ReadingProgress => ({
  book_id: 7,
  progress: 36,
  progression: 0.36,
  active: 1,
  completed: false,
  file_id: fileId,
  locator: { value: 'epubcfi(/6/4)', progression: 0.36 },
  reader_updated_at: '2026-07-29T08:00:00Z',
  source: 'web',
  revision: 1,
  updated_at: '2026-07-29T08:00:00Z',
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('reader progress restore selection', () => {
  it('prefers the exact local locator over server progress', () => {
    expect(
      readerLocatorForOpen(
        { value: 'epubcfi(/6/8)', progression: 0.5 },
        {
          value: 'epubcfi(/6/4)',
          progression: 0.36,
          completed: false,
          fileId: 36,
        },
      ),
    ).toEqual({ value: 'epubcfi(/6/8)', progression: 0.5 });
  });

  it('uses the server locator when no local locator exists', () => {
    expect(
      readerLocatorForOpen(null, {
        value: 'page:36',
        progression: 0.5,
        completed: false,
        fileId: 36,
      }),
    ).toEqual({ value: 'page:36', progression: 0.5 });
  });

  it('does not load a local locator saved for another file', () => {
    const values = new Map<string, string>();
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value),
      },
    });

    saveReaderLocator(7, 36, { value: 'page:36', progression: 0.5 });

    expect(loadReaderLocator(7, 36)).toEqual({ value: 'page:36', progression: 0.5 });
    expect(loadReaderLocator(7, 37)).toBeNull();
  });

  it('keeps server percentage but drops an exact locator from another file', async () => {
    vi.spyOn(v1Api, 'readingProgressForBook').mockResolvedValue({
      progress: serverProgress(36),
    });

    await expect(getReaderProgress(7, 37)).resolves.toMatchObject({
      value: '',
      progression: 0.36,
      fileId: 36,
    });
  });

  it('keeps exact page presentation data out of the server synchronization contract', async () => {
    const setProgress = vi.spyOn(v1Api, 'setReadingProgress').mockResolvedValue({
      progress: serverProgress(36),
    });

    await saveReaderProgress(7, 36, {
      value: 'page:36',
      progression: 0.18,
      currentPage: 36,
      totalPages: 200,
      completed: false,
      updatedAt: '2026-07-29T08:00:00Z',
    });

    expect(setProgress).toHaveBeenCalledWith(7, 0.18, false, {
      fileId: 36,
      locator: { value: 'page:36', progression: 0.18 },
      clientUpdatedAt: '2026-07-29T08:00:00Z',
      completed: false,
    });
  });
});
