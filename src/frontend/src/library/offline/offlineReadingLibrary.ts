import type { OfflineReadingStore } from './offlineReadingStore';
import type {
  CacheBookInput,
  CacheFetchInput,
  CacheReconciliation,
  OfflineBook,
  ProgressUpdate,
  RefreshResult,
} from './types';

const MAX_OFFLINE_BOOKS = 3;

const nowIso = (): string => new Date().toISOString();

const defaultProgress = (timestamp: string) => ({
  locator: '',
  progression: 0,
  completed: false,
  lastReadAt: timestamp,
});

const normaliseProgress = (progress: ProgressUpdate, timestamp: string) => ({
  locator: progress.locator,
  progression: Math.min(1, Math.max(0, progress.progression)),
  completed: progress.completed,
  lastReadAt: progress.lastReadAt ?? timestamp,
});

const isNewer = (left: string, right: string): boolean => Date.parse(left) > Date.parse(right);

export type ContentFetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/**
 * Caches reader files in IndexedDB and owns the "three active books" policy.
 * It has no authentication knowledge: callers must supply a stable account id.
 */
export class OfflineReadingLibrary {
  constructor(
    private readonly store: OfflineReadingStore,
    private readonly fetcher: ContentFetcher = fetch,
    private readonly clock: () => string = nowIso,
  ) {}

  async cacheBook(input: CacheBookInput, protectedFileIds: ReadonlySet<number> = new Set()) {
    const timestamp = this.clock();
    const current = await this.store.getBook(input.accountId, input.fileId);
    const book: OfflineBook = {
      ...input,
      cover: input.cover ?? null,
      etag: input.etag ?? current?.etag ?? null,
      contentType: input.contentType ?? current?.contentType ?? (input.content.type || null),
      lastAccessedAt: timestamp,
      progress: input.progress ?? current?.progress ?? defaultProgress(timestamp),
    };
    // Continue Reading never keeps completed files. Completion changes made by
    // the reader go through recordProgress(), which also queues the update.
    if (book.progress.completed) {
      await this.store.deleteBook(input.accountId, input.fileId);
      return { evictedFileIds: [input.fileId], deferredFileIds: [] };
    }
    await this.store.putBook(book);
    return this.reconcile(input.accountId, protectedFileIds);
  }

  /** Downloads an entire file before exposing it as offline available. */
  async downloadAndCache(
    input: CacheFetchInput,
    protectedFileIds: ReadonlySet<number> = new Set(),
  ) {
    const fetcher = this.fetcher;
    const response = await fetcher(input.contentUrl, { credentials: 'include' });
    if (!response.ok) {
      throw new Error(`Unable to cache ${input.title}: ${response.status} ${response.statusText}`);
    }
    const content = await response.blob();
    return this.cacheBook(
      {
        ...input,
        content,
        etag: response.headers.get('ETag'),
        contentType: response.headers.get('Content-Type'),
      },
      protectedFileIds,
    );
  }

  async getBook(accountId: string, fileId: number): Promise<OfflineBook | null> {
    return this.store.getBook(accountId, fileId);
  }

  async listContinueReading(accountId: string): Promise<OfflineBook[]> {
    const books = await this.store.listBooks(accountId);
    return books
      .filter((book) => !book.progress.completed)
      .toSorted(
        (left, right) =>
          Date.parse(right.progress.lastReadAt) - Date.parse(left.progress.lastReadAt),
      );
  }

  /**
   * Records a local reader action immediately. A separate queue is retained
   * until the server accepts it, so closing the PWA while offline is safe.
   */
  async recordProgress(
    accountId: string,
    bookId: number,
    fileId: number,
    update: ProgressUpdate,
    protectedFileIds: ReadonlySet<number> = new Set(),
  ): Promise<CacheReconciliation> {
    const timestamp = this.clock();
    const progress = normaliseProgress(update, timestamp);
    const existing = await this.store.getBook(accountId, fileId);

    await this.store.putPendingProgress({ accountId, bookId, fileId, ...progress });

    if (!existing) return { evictedFileIds: [], deferredFileIds: [] };
    if (progress.completed) {
      // The progress queue remains; only the bulky offline content is removed.
      await this.store.deleteBook(accountId, fileId);
      return { evictedFileIds: [fileId], deferredFileIds: [] };
    }
    await this.store.putBook({ ...existing, progress, lastAccessedAt: progress.lastReadAt });
    return this.reconcile(accountId, protectedFileIds);
  }

  /**
   * Revalidates an existing offline file with If-None-Match when the PWA is
   * online. A response body is only stored after it has been read completely.
   */
  async refreshBook(accountId: string, fileId: number): Promise<RefreshResult> {
    const cached = await this.store.getBook(accountId, fileId);
    if (!cached) return { changed: false, etag: null };
    const headers = new Headers();
    if (cached.etag) headers.set('If-None-Match', cached.etag);
    const fetcher = this.fetcher;
    const response = await fetcher(cached.contentUrl, { credentials: 'include', headers });
    if (response.status === 304) return { changed: false, etag: cached.etag };
    if (!response.ok) {
      throw new Error(
        `Unable to refresh ${cached.title}: ${response.status} ${response.statusText}`,
      );
    }
    const content = await response.blob();
    const etag = response.headers.get('ETag');
    await this.store.putBook({
      ...cached,
      content,
      etag,
      contentType: response.headers.get('Content-Type') ?? cached.contentType,
    });
    return { changed: true, etag };
  }

  async clearAccount(accountId: string): Promise<void> {
    await this.store.clearAccount(accountId);
  }

  /** Remove older inactive files, except a file that is currently open. */
  async reconcile(
    accountId: string,
    protectedFileIds: ReadonlySet<number> = new Set(),
  ): Promise<CacheReconciliation> {
    const candidates = (await this.store.listBooks(accountId))
      .filter((book) => !book.progress.completed)
      .toSorted(
        (left, right) => Date.parse(left.lastAccessedAt) - Date.parse(right.lastAccessedAt),
      );
    const evictedFileIds: number[] = [];
    const deferredFileIds: number[] = [];
    let activeCount = candidates.length;
    const removals: Promise<void>[] = [];
    for (const book of candidates) {
      if (activeCount <= MAX_OFFLINE_BOOKS) break;
      if (protectedFileIds.has(book.fileId)) {
        deferredFileIds.push(book.fileId);
        continue;
      }
      removals.push(this.store.deleteBook(accountId, book.fileId));
      evictedFileIds.push(book.fileId);
      activeCount -= 1;
    }
    await Promise.all(removals);
    return { evictedFileIds, deferredFileIds };
  }
}

export const isServerProgressNewer = (serverTimestamp: string, localTimestamp: string): boolean =>
  isNewer(serverTimestamp, localTimestamp);
