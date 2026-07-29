import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  MemoryOfflineReadingStore,
  OfflineReadingLibrary,
  sendPendingProgress,
  syncOfflineProgress,
  type CacheBookInput,
  type ContentFetcher,
} from '../library/offline';
import { v1Api } from '../services/v1Api';

const accountId = 'user-42';
const baseInput = (fileId: number): CacheBookInput => ({
  accountId,
  bookId: 7,
  fileId,
  title: `Book ${fileId}`,
  author: 'Author',
  format: 'EPUB',
  coverUrl: null,
  contentUrl: `/api/library/books/7/files/${fileId}/content`,
  content: new Blob([`book ${fileId}`], { type: 'application/epub+zip' }),
});

const timestamps = [
  '2026-07-01T00:00:00.000Z',
  '2026-07-02T00:00:00.000Z',
  '2026-07-03T00:00:00.000Z',
  '2026-07-04T00:00:00.000Z',
];

const timestampAt = (index: number): string => timestamps[index] ?? timestamps.at(-1) ?? '';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('OfflineReadingLibrary', () => {
  it('keeps only the three most recently active incomplete books', async () => {
    const store = new MemoryOfflineReadingStore();
    let index = 0;
    const library = new OfflineReadingLibrary(store, fetch, () => timestampAt(index++));

    await library.cacheBook(baseInput(1));
    await library.cacheBook(baseInput(2));
    await library.cacheBook(baseInput(3));
    const result = await library.cacheBook(baseInput(4));

    expect(result).toEqual({ evictedFileIds: [1], deferredFileIds: [] });
    expect((await library.listContinueReading(accountId)).map((book) => book.fileId)).toEqual([
      4, 3, 2,
    ]);
  });

  it('defers eviction of an open reader until it closes', async () => {
    const store = new MemoryOfflineReadingStore();
    let index = 0;
    const library = new OfflineReadingLibrary(store, fetch, () => timestampAt(index++));

    await library.cacheBook(baseInput(1));
    await library.cacheBook(baseInput(2));
    await library.cacheBook(baseInput(3));
    const protectedFiles = new Set([1]);
    const result = await library.cacheBook(baseInput(4), protectedFiles);

    expect(result).toEqual({ evictedFileIds: [2], deferredFileIds: [1] });
    expect((await library.listContinueReading(accountId)).map((book) => book.fileId)).toEqual([
      4, 3, 1,
    ]);
  });

  it('queues completed progress but releases the bulky offline book', async () => {
    const store = new MemoryOfflineReadingStore();
    const library = new OfflineReadingLibrary(store, fetch, () => timestampAt(0));
    await library.cacheBook(baseInput(1));

    const result = await library.recordProgress(accountId, 7, 1, {
      locator: 'epubcfi(/6/4)',
      progression: 1,
      completed: true,
    });

    expect(result).toEqual({ evictedFileIds: [1], deferredFileIds: [] });
    expect(await library.getBook(accountId, 1)).toBeNull();
    expect(await store.getPendingProgress(accountId, 1)).toMatchObject({ completed: true });
  });

  it('uses an ETag to avoid replacing an unchanged cached file', async () => {
    const store = new MemoryOfflineReadingStore();
    const requests: RequestInit[] = [];
    const fetcher: ContentFetcher = async (_input, init) => {
      requests.push(init ?? {});
      return new Response(null, { status: 304 });
    };
    const library = new OfflineReadingLibrary(store, fetcher, () => timestampAt(0));
    await library.cacheBook({ ...baseInput(1), etag: '"v1"' });

    await expect(library.refreshBook(accountId, 1)).resolves.toEqual({
      changed: false,
      etag: '"v1"',
    });
    expect(new Headers(requests[0]?.headers).get('If-None-Match')).toBe('"v1"');
  });
});

describe('syncOfflineProgress', () => {
  it('maps a recorded offline locator and progression through the real V1 sender', async () => {
    const store = new MemoryOfflineReadingStore();
    const library = new OfflineReadingLibrary(store, fetch, () => timestampAt(0));
    await library.cacheBook(baseInput(1));
    await library.recordProgress(accountId, 7, 1, {
      locator: 'page:36',
      progression: 0.18,
      completed: false,
    });
    const setProgress = vi.spyOn(v1Api, 'setReadingProgress').mockResolvedValue({
      progress: {
        book_id: 7,
        progress: 18,
        progression: 0.18,
        active: 1,
        completed: false,
        file_id: 1,
        locator: { value: 'page:36', progression: 0.18 },
        reader_updated_at: timestamps[0],
        source: 'web',
        revision: 1,
        updated_at: timestamps[0],
      },
    });

    await expect(syncOfflineProgress(store, accountId, sendPendingProgress)).resolves.toEqual({
      sent: 1,
      discardedAsStale: 0,
      failed: 0,
    });
    expect(setProgress).toHaveBeenCalledWith(7, 0.18, false, {
      fileId: 1,
      locator: { value: 'page:36', progression: 0.18 },
      clientUpdatedAt: timestamps[0],
      completed: false,
    });
    expect(await store.getPendingProgress(accountId, 1)).toBeNull();
  });

  it('keeps a newer server position and discards the stale local queue entry', async () => {
    const store = new MemoryOfflineReadingStore();
    await store.putPendingProgress({
      accountId,
      bookId: 7,
      fileId: 1,
      locator: 'old',
      progression: 0.1,
      completed: false,
      lastReadAt: '2026-07-01T00:00:00.000Z',
    });

    const result = await syncOfflineProgress(store, accountId, async () => ({
      progress: {
        locator: 'newer-server',
        progression: 0.5,
        completed: false,
        lastReadAt: '2026-07-02T00:00:00.000Z',
      },
    }));

    expect(result).toEqual({ sent: 0, discardedAsStale: 1, failed: 0 });
    expect(await store.getPendingProgress(accountId, 1)).toBeNull();
  });

  it('retains a newer local action written while an older request is in flight', async () => {
    const store = new MemoryOfflineReadingStore();
    await store.putPendingProgress({
      accountId,
      bookId: 7,
      fileId: 1,
      locator: 'first',
      progression: 0.1,
      completed: false,
      lastReadAt: '2026-07-01T00:00:00.000Z',
    });

    const result = await syncOfflineProgress(store, accountId, async () => {
      await store.putPendingProgress({
        accountId,
        bookId: 7,
        fileId: 1,
        locator: 'newer-local',
        progression: 0.2,
        completed: false,
        lastReadAt: '2026-07-03T00:00:00.000Z',
      });
      return { progress: null };
    });

    expect(result).toEqual({ sent: 1, discardedAsStale: 0, failed: 0 });
    expect(await store.getPendingProgress(accountId, 1)).toMatchObject({ locator: 'newer-local' });
  });
});
