import type { ReactNode } from 'react';
import { createContext, useCallback, useContext, useMemo, useState } from 'react';

import { useNetworkStatus } from '../../contexts/NetworkStatusContext';
import { useDependencyEffect } from '../../hooks/useMountEffect';
import { canReadInBrowser } from '../../reader/formatCapabilities';
import { readerFileFromV1 } from '../../reader/readerApi';
import { v1Api } from '../../services/v1Api';
import type { V1BookDetail, V1Page, V1ReadingProgress } from '../../types/v1';
import { startOfflineProgressSync } from './offlineProgressSync';
import type { OfflineReadingLibrary } from './offlineReadingLibrary';
import type { OfflineReadingStore } from './offlineReadingStore';
import {
  currentOpenReaderFileIds,
  offlineReadingLibrary,
  offlineReadingStore,
  progressFromV1,
  sendPendingProgress,
} from './offlineRuntime';
import type { OfflineBook, OfflineCacheErrorCode, OfflineCacheOutcome } from './types';

interface OfflineReadingApi {
  readingProgress: () => Promise<V1Page<V1ReadingProgress>>;
  book: (bookId: number) => Promise<V1BookDetail>;
}

export interface OfflineCacheSyncResult {
  books: OfflineBook[];
  outcomes: OfflineCacheOutcome[];
}

const MAX_OFFLINE_BOOKS = 3;
const MAX_OFFLINE_CANDIDATES = 12;

export const selectOfflineReadingCandidates = (items: V1ReadingProgress[]): V1ReadingProgress[] =>
  items
    .filter((item) => item.active === 1 && (item.progression ?? item.progress / 100) < 1)
    .toSorted(
      (left, right) =>
        Date.parse(right.reader_updated_at ?? right.updated_at) -
        Date.parse(left.reader_updated_at ?? left.updated_at),
    )
    // A repeated progress record must not consume another cache slot.
    .filter(
      (item, index, sorted) =>
        sorted.findIndex((other) => other.book_id === item.book_id) === index,
    )
    // Do not scan an unbounded reading history when several entries are invalid.
    .slice(0, MAX_OFFLINE_CANDIDATES);

const statusFromError = (error: unknown): number | null => {
  if (typeof error !== 'object' || error === null || !('status' in error)) return null;
  const status = (error as { status?: unknown }).status;
  return typeof status === 'number' ? status : null;
};

const classifyError = (error: unknown, phase: 'detail' | 'download'): OfflineCacheErrorCode => {
  const status = statusFromError(error);
  if (status === 401 || status === 403) return 'not_entitled';
  if (status === 404) return phase === 'detail' ? 'detail_unavailable' : 'file_missing';
  const name = error instanceof Error ? error.name : '';
  const message = error instanceof Error ? error.message.toLowerCase() : '';
  if (name === 'QuotaExceededError' || message.includes('quota')) return 'storage_quota';
  if (message.includes('indexeddb') || name === 'InvalidStateError') return 'indexeddb_failed';
  return phase === 'detail' ? 'detail_unavailable' : 'download_failed';
};

const isPermanent = (code: OfflineCacheErrorCode): boolean =>
  code === 'not_entitled' || code === 'file_missing' || code === 'unsupported_format';

const failureFingerprint = (accountId: string, outcome: OfflineCacheOutcome): string =>
  `${accountId}:${outcome.bookId}:${outcome.fileId ?? 'none'}:${outcome.errorCode ?? 'none'}`;

/**
 * Keep current failure state separate from notification de-duplication.
 * The returned fingerprint set deliberately drops recovered, replaced, and
 * ineligible entries so a later recurrence can be reported as a new cycle.
 */
export const reconcileOfflineFailureFingerprints = (
  accountId: string,
  outcomes: OfflineCacheOutcome[],
  previous: ReadonlySet<string>,
): {
  failures: OfflineCacheOutcome[];
  newFailures: OfflineCacheOutcome[];
  fingerprints: Set<string>;
} => {
  const retryable = outcomes.filter((outcome) => outcome.status === 'temporarily_failed');
  const fingerprints = new Set(retryable.map((outcome) => failureFingerprint(accountId, outcome)));
  return {
    failures: retryable,
    newFailures: retryable.filter(
      (outcome) => !previous.has(failureFingerprint(accountId, outcome)),
    ),
    fingerprints,
  };
};

const failureStorageKey = (accountId: string): string =>
  `bookflow:offline-cache-failures:${accountId}`;

const readFailureFingerprints = (accountId: string): Set<string> => {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(failureStorageKey(accountId)) ?? '[]');
    return new Set(
      Array.isArray(parsed)
        ? parsed.filter((value): value is string => typeof value === 'string')
        : [],
    );
  } catch {
    return new Set();
  }
};

const saveFailureFingerprints = (accountId: string, fingerprints: ReadonlySet<string>): void => {
  try {
    localStorage.setItem(failureStorageKey(accountId), JSON.stringify([...fingerprints]));
  } catch {
    // Private mode or a full browser store must not make reader caching fail.
  }
};

/**
 * Cache candidates oldest-to-newest so the newest activity wins eviction.
 * Each candidate is isolated: one unavailable file must not block the rest.
 */
export const syncOfflineContinueReading = async (
  accountId: string,
  api: OfflineReadingApi = v1Api,
  library: OfflineReadingLibrary = offlineReadingLibrary,
  store: OfflineReadingStore = offlineReadingStore,
  protectedFileIds: () => ReadonlySet<number> = currentOpenReaderFileIds,
): Promise<OfflineCacheSyncResult> => {
  const page = await api.readingProgress();
  const candidates = selectOfflineReadingCandidates(page.items);
  const desiredBookIds = new Set<number>();
  const desiredFileIds = new Set<number>();
  const outcomes: OfflineCacheOutcome[] = [];
  let fulfilled = 0;

  /* oxlint-disable no-await-in-loop -- deterministic cache eviction requires ordered writes */
  for (const progress of candidates.toReversed()) {
    if (fulfilled >= MAX_OFFLINE_BOOKS) break;
    let detail: V1BookDetail;
    try {
      detail = await api.book(progress.book_id);
    } catch (error) {
      const errorCode = classifyError(error, 'detail');
      outcomes.push({
        bookId: progress.book_id,
        fileId: progress.file_id ?? null,
        title: progress.title ?? null,
        status: isPermanent(errorCode) ? 'permanently_ineligible' : 'temporarily_failed',
        errorCode,
      });
      continue;
    }
    const file =
      detail.files.find(
        (entry) => entry.id === progress.file_id && canReadInBrowser(entry.file_format),
      ) ?? detail.files.find((entry) => canReadInBrowser(entry.file_format));
    if (!file) {
      outcomes.push({
        bookId: progress.book_id,
        fileId: progress.file_id ?? null,
        title: detail.book.title,
        status: 'permanently_ineligible',
        errorCode: detail.files.length ? 'unsupported_format' : 'file_missing',
      });
      continue;
    }
    const readerFile = readerFileFromV1({
      bookId: progress.book_id,
      fileId: file.id,
      title: detail.book.title,
      author: detail.book.author,
      format: file.file_format,
    });
    const metadata = {
      accountId,
      bookId: progress.book_id,
      fileId: file.id,
      title: detail.book.title,
      author: detail.book.author,
      format: file.file_format,
      coverUrl: detail.book.cover_url,
      contentUrl: readerFile.contentUrl,
      progress: progressFromV1(progress),
    };
    try {
      const cached = await library.getBook(accountId, file.id);
      if (cached) {
        await library.cacheBook({ ...cached, ...metadata }, protectedFileIds());
        try {
          await library.refreshBook(accountId, file.id);
          outcomes.push({
            bookId: metadata.bookId,
            fileId: metadata.fileId,
            title: metadata.title,
            status: 'cached',
            errorCode: null,
          });
        } catch (error) {
          const errorCode = classifyError(error, 'download');
          // cacheBook has already safely retained the old complete Blob.
          desiredBookIds.add(progress.book_id);
          desiredFileIds.add(file.id);
          fulfilled += 1;
          outcomes.push({
            bookId: metadata.bookId,
            fileId: metadata.fileId,
            title: metadata.title,
            status: 'kept_existing_copy',
            errorCode,
          });
          continue;
        }
      } else {
        await library.downloadAndCache(metadata, protectedFileIds());
        outcomes.push({
          bookId: metadata.bookId,
          fileId: metadata.fileId,
          title: metadata.title,
          status: 'cached',
          errorCode: null,
        });
      }
      desiredBookIds.add(progress.book_id);
      desiredFileIds.add(file.id);
      fulfilled += 1;
    } catch (error) {
      const errorCode = classifyError(error, 'download');
      outcomes.push({
        bookId: progress.book_id,
        fileId: file.id,
        title: detail.book.title,
        status: isPermanent(errorCode) ? 'permanently_ineligible' : 'temporarily_failed',
        errorCode,
      });
    }
  }
  /* oxlint-enable no-await-in-loop */

  const cachedBooks = await library.listContinueReading(accountId);
  await Promise.all(
    cachedBooks
      .filter(
        (book) =>
          !desiredFileIds.has(book.fileId) &&
          !desiredBookIds.has(book.bookId) &&
          !protectedFileIds().has(book.fileId),
      )
      .map((book) => store.deleteBook(accountId, book.fileId)),
  );
  return {
    books: await library.listContinueReading(accountId),
    outcomes,
  };
};

type OfflineCacheStatus = 'loading' | 'syncing' | 'ready' | 'error';

export const offlineCacheStatusFromFailures = (
  failures: readonly OfflineCacheOutcome[],
): OfflineCacheStatus => (failures.length ? 'error' : 'ready');

interface OfflineReadingContextValue {
  books: OfflineBook[];
  cachedFileIds: ReadonlySet<number>;
  status: OfflineCacheStatus;
  failedBookCount: number;
  failures: OfflineCacheOutcome[];
  refreshBooks: () => Promise<void>;
  retryOfflineCache: () => Promise<void>;
}

const OfflineReadingContext = createContext<OfflineReadingContextValue>({
  books: [],
  cachedFileIds: new Set(),
  status: 'loading',
  failedBookCount: 0,
  failures: [],
  refreshBooks: async () => undefined,
  retryOfflineCache: async () => undefined,
});

export const useOfflineReading = (): OfflineReadingContextValue =>
  useContext(OfflineReadingContext);

const reconcileStoredFailureState = (
  accountId: string,
  outcomes: OfflineCacheOutcome[],
): ReturnType<typeof reconcileOfflineFailureFingerprints> => {
  const reconciled = reconcileOfflineFailureFingerprints(
    accountId,
    outcomes,
    readFailureFingerprints(accountId),
  );
  saveFailureFingerprints(accountId, reconciled.fingerprints);
  return reconciled;
};

export const OfflineReadingProvider = ({
  accountId,
  children,
}: {
  accountId: string;
  children: ReactNode;
}) => {
  const { isOnline } = useNetworkStatus();
  const [books, setBooks] = useState<OfflineBook[]>([]);
  const [status, setStatus] = useState<OfflineCacheStatus>('loading');
  const [failedBookCount, setFailedBookCount] = useState(0);
  const [failures, setFailures] = useState<OfflineCacheOutcome[]>([]);

  const refreshBooks = useCallback(async () => {
    try {
      const cached = await offlineReadingLibrary.listContinueReading(accountId);
      setBooks(cached);
      setStatus((current) => (current === 'syncing' ? current : 'ready'));
    } catch {
      setStatus('error');
    }
  }, [accountId]);

  useDependencyEffect(() => {
    let cancelled = false;
    void offlineReadingLibrary
      .listContinueReading(accountId)
      .then((cached) => {
        if (cancelled) return;
        setBooks(cached);
        setStatus('ready');
      })
      .catch(() => {
        if (!cancelled) setStatus('error');
      });
    return () => {
      cancelled = true;
    };
  }, [accountId]);

  useDependencyEffect(() => {
    const sync = startOfflineProgressSync(offlineReadingStore, accountId, sendPendingProgress);
    return () => sync.stop();
  }, [accountId]);

  useDependencyEffect(() => {
    if (!isOnline) return undefined;
    let cancelled = false;
    let syncing = false;
    const updateCache = async () => {
      if (syncing) return;
      syncing = true;
      setStatus('syncing');
      try {
        const result = await syncOfflineContinueReading(accountId);
        if (cancelled) return;
        setBooks(result.books);
        const { failures: retryableFailures } = reconcileStoredFailureState(
          accountId,
          result.outcomes,
        );
        setFailures(retryableFailures);
        setFailedBookCount(retryableFailures.length);
        setStatus(offlineCacheStatusFromFailures(retryableFailures));
      } catch {
        if (!cancelled) {
          setFailedBookCount(0);
          setFailures([]);
          setStatus('error');
        }
      } finally {
        syncing = false;
      }
    };
    const updateWhenVisible = () => {
      if (document.visibilityState === 'visible') void updateCache();
    };
    void updateCache();
    document.addEventListener('visibilitychange', updateWhenVisible);
    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', updateWhenVisible);
    };
  }, [accountId, isOnline]);

  const retryOfflineCache = useCallback(async () => {
    if (!isOnline) return;
    setStatus('syncing');
    const result = await syncOfflineContinueReading(accountId);
    const { failures: retryableFailures } = reconcileStoredFailureState(accountId, result.outcomes);
    setBooks(result.books);
    setFailures(retryableFailures);
    setFailedBookCount(retryableFailures.length);
    setStatus(offlineCacheStatusFromFailures(retryableFailures));
  }, [accountId, isOnline]);

  const value = useMemo(
    () => ({
      books,
      cachedFileIds: new Set(books.map((book) => book.fileId)),
      status,
      failedBookCount,
      failures,
      refreshBooks,
      retryOfflineCache,
    }),
    [books, failedBookCount, failures, refreshBooks, retryOfflineCache, status],
  );

  return <OfflineReadingContext.Provider value={value}>{children}</OfflineReadingContext.Provider>;
};
