import { useCallback, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { useLanguage } from '../contexts/LanguageContext';
import { useDependencyEffect } from '../hooks/useMountEffect';
import {
  offlineReadingLibrary,
  offlineReadingStore,
  protectOfflineReaderFile,
  sendPendingProgress,
  syncOfflineProgress,
} from '../library/offline';
import { v1Api } from '../services/v1Api';
import { readerAdapterFactory } from './adapters/readerAdapterFactory';
import { BrowserReader } from './components/BrowserReader';
import { bookFileFromBlob, downloadBookFile, fetchBookFile, shareBookFile } from './fileActions';
import { canReadInBrowser } from './formatCapabilities';
import { getReaderProgress, readerFileFromV1, readerLocatorForOpen } from './readerApi';
import { loadReaderLocator, saveReaderLocator } from './readerLocatorStore';
import type { ReaderFile, ReaderLocator, ReaderProgress } from './types';

const positiveId = (value: string | undefined): number | null => {
  const id = Number(value);
  return Number.isInteger(id) && id > 0 ? id : null;
};

/**
 * Route-level orchestration is intentionally separate from App.tsx so the
 * reader can be wired into the route table after both engine dependencies are
 * added. It only needs V1BookFile IDs and the existing V1 progress endpoints.
 */
interface ReaderPageProps {
  accountId: string;
}

interface ReaderOpenState {
  file: ReaderFile;
  /** `null` is a completed decision: it means no previous locator exists. */
  initialLocator: ReaderLocator | null;
}

export const ReaderPage = ({ accountId }: ReaderPageProps) => {
  const { t } = useLanguage();
  const { bookId: rawBookId, fileId: rawFileId } = useParams();
  const navigate = useNavigate();
  const [openState, setOpenState] = useState<ReaderOpenState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionFileState, setActionFileState] = useState<'preparing' | 'ready' | 'failed'>(
    'preparing',
  );
  const pendingProgress = useRef<ReaderProgress | null>(null);
  const timer = useRef<number | null>(null);
  const objectUrl = useRef<string | null>(null);
  const preparedActionFile = useRef<File | null>(null);

  const bookId = positiveId(rawBookId);
  const fileId = positiveId(rawFileId);
  const file = openState?.file ?? null;

  const flushProgress = useCallback(async () => {
    if (!bookId || !fileId || !pendingProgress.current) return;
    const progress = pendingProgress.current;
    pendingProgress.current = null;
    try {
      await offlineReadingLibrary.recordProgress(accountId, bookId, fileId, {
        locator: progress.value,
        progression: progress.progression,
        completed: progress.completed,
        lastReadAt: progress.updatedAt,
      });
      const result = await syncOfflineProgress(offlineReadingStore, accountId, sendPendingProgress);
      if (result.failed > 0) pendingProgress.current = progress;
    } catch {
      pendingProgress.current = progress;
    }
  }, [accountId, bookId, fileId]);

  useDependencyEffect(() => {
    if (!bookId || !fileId) {
      setError('The book or file link is invalid.');
      return undefined;
    }
    let cancelled = false;
    setError(null);
    setOpenState(null);
    const releaseOfflineProtection = protectOfflineReaderFile(fileId);
    const openCached = async () => {
      const cached = await offlineReadingLibrary.getBook(accountId, fileId);
      if (!cached || cancelled) return false;
      objectUrl.current = URL.createObjectURL(cached.content);
      setOpenState({
        file: {
          bookId: cached.bookId,
          fileId: cached.fileId,
          title: cached.title,
          author: cached.author,
          format: cached.format,
          contentUrl: objectUrl.current,
          etag: cached.etag,
        },
        initialLocator: {
          value: cached.progress.locator,
          progression: cached.progress.progression,
        },
      });
      return true;
    };

    void Promise.all([v1Api.book(bookId), getReaderProgress(bookId, fileId)]).then(
      async ([detail, serverProgress]) => {
        if (cancelled) return;
        const selected = detail.files.find((entry) => entry.id === fileId);
        if (!selected) {
          setError('This file is no longer available in your library.');
          return;
        }
        if (!canReadInBrowser(selected.file_format)) {
          setError(`${selected.file_format.toUpperCase()} is not supported by the browser reader.`);
          return;
        }
        const readerFile = readerFileFromV1({
          bookId,
          fileId,
          title: detail.book.title,
          author: detail.book.author,
          format: selected.file_format,
        });
        setOpenState({
          file: readerFile,
          initialLocator: readerLocatorForOpen(loadReaderLocator(bookId, fileId), serverProgress),
        });
        const cached = await offlineReadingLibrary.getBook(accountId, fileId);
        const progress = serverProgress
          ? {
              locator: serverProgress.value,
              progression: serverProgress.progression,
              completed: serverProgress.completed,
              lastReadAt: serverProgress.updatedAt ?? new Date().toISOString(),
            }
          : undefined;
        const metadata = {
          accountId,
          bookId,
          fileId,
          title: detail.book.title,
          author: detail.book.author,
          format: selected.file_format,
          coverUrl: detail.book.cover_url,
          contentUrl: readerFile.contentUrl,
          progress,
        };
        void (
          cached
            ? offlineReadingLibrary.refreshBook(accountId, fileId)
            : offlineReadingLibrary.downloadAndCache(metadata, new Set([fileId]))
        ).catch(() => undefined);
      },
      async () => {
        if (!cancelled && !(await openCached())) setError('The book could not be loaded offline.');
      },
    );
    return () => {
      cancelled = true;
      releaseOfflineProtection();
      if (objectUrl.current) {
        URL.revokeObjectURL(objectUrl.current);
        objectUrl.current = null;
      }
    };
  }, [accountId, bookId, fileId]);

  useDependencyEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      void flushProgress();
    },
    [flushProgress],
  );

  const scheduleProgress = useCallback(
    (locator: ReaderLocator) => {
      if (!bookId || !fileId) return;
      saveReaderLocator(bookId, fileId, locator);
      pendingProgress.current = {
        ...locator,
        completed: locator.progression >= 0.999,
        updatedAt: new Date().toISOString(),
      };
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        timer.current = null;
        void flushProgress();
      }, 1500);
    },
    [bookId, fileId, flushProgress],
  );

  const loadActionFile = useCallback(async (): Promise<File> => {
    if (!bookId || !fileId || !file) throw new Error('Het boekbestand is niet beschikbaar.');
    const cached = await offlineReadingLibrary.getBook(accountId, fileId);
    if (cached) {
      return bookFileFromBlob(cached.content, file.title, file.format, cached.contentType);
    }
    return fetchBookFile(v1Api.libraryFileDownloadUrl(bookId, fileId), file.title, file.format);
  }, [accountId, bookId, file, fileId]);

  // Keep an actual File, rather than a promise, ready before the toolbar is
  // tapped. Android Chrome's Web Share API requires the call to happen in the
  // original transient user-activation task.
  useDependencyEffect(() => {
    let cancelled = false;
    preparedActionFile.current = null;
    if (!file) return undefined;
    setActionFileState('preparing');
    void loadActionFile().then(
      (preparedFile) => {
        if (cancelled) return;
        preparedActionFile.current = preparedFile;
        setActionFileState('ready');
      },
      () => {
        if (!cancelled) setActionFileState('failed');
      },
    );
    return () => {
      cancelled = true;
      preparedActionFile.current = null;
    };
  }, [file, loadActionFile]);

  if (error) {
    return (
      <div className="p-8">
        <p className="text-sm">{error}</p>
        <button
          type="button"
          className="mt-3 rounded bg-sky-700 px-3 py-2 text-sm text-white"
          onClick={() => {
            void navigate('/library');
          }}
        >
          Back to library
        </button>
      </div>
    );
  }

  if (!file) return <p className="p-8 text-sm opacity-70">Opening book…</p>;

  return (
    <BrowserReader
      file={file}
      initialLocator={openState?.initialLocator}
      adapterFactory={readerAdapterFactory}
      onBack={() => {
        void navigate(`/library/${file.bookId}`);
      }}
      onProgressChange={scheduleProgress}
      onDownload={async () =>
        downloadBookFile(preparedActionFile.current ?? (await loadActionFile()))
      }
      onShare={() => {
        const preparedFile = preparedActionFile.current;
        if (preparedFile) return shareBookFile(preparedFile, file.title);
        const status =
          actionFileState === 'failed'
            ? t('catalogDetail.sharePreparationFailed')
            : t('catalogDetail.sharePreparationPending');
        return Promise.reject(new Error(status));
      }}
    />
  );
};
