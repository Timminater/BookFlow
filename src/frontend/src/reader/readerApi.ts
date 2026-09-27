import { v1Api } from '../services/v1Api';
import { getApiBase } from '../utils/basePath';
import type { ReaderFile, ReaderLocator, ReaderProgress } from './types';

const locatorValue = (locator: unknown): string => {
  if (!locator || typeof locator !== 'object' || !('value' in locator)) return '';
  return typeof locator.value === 'string' ? locator.value : '';
};

const readerContentUrl = (bookId: number, fileId: number): string =>
  `${getApiBase()}/v1/library/books/${bookId}/files/${fileId}/content`;

/**
 * Reading progress remains book-scoped for dashboard and Kobo compatibility.
 * The file ID guards exact locators while device-local positions can take
 * precedence for the same file.
 */
export const getReaderProgress = async (
  bookId: number,
  fileId: number,
): Promise<ReaderProgress | null> => {
  try {
    const { progress } = await v1Api.readingProgressForBook(bookId);
    const exactFile = progress.file_id === fileId;
    return {
      // The API keeps progress book-scoped for dashboard/Kobo compatibility,
      // but an exact locator is only valid for the file that produced it.
      value: exactFile ? locatorValue(progress.locator) : '',
      progression: progress.progression ?? progress.progress / 100,
      completed: progress.completed ?? progress.active === 0,
      updatedAt: progress.reader_updated_at ?? progress.updated_at,
      fileId: progress.file_id ?? null,
    };
  } catch {
    return null;
  }
};

export const readerLocatorForOpen = (
  localLocator: ReaderLocator | null,
  serverProgress: ReaderProgress | null,
): ReaderLocator | null => {
  const localTime = Date.parse(localLocator?.updatedAt ?? '');
  const serverTime = Date.parse(serverProgress?.updatedAt ?? '');
  if (
    serverProgress &&
    Number.isFinite(serverTime) &&
    (!Number.isFinite(localTime) || serverTime > localTime)
  ) {
    return { value: serverProgress.value, progression: serverProgress.progression };
  }
  return (
    localLocator ??
    (serverProgress
      ? { value: serverProgress.value, progression: serverProgress.progression }
      : null)
  );
};

export const saveReaderProgress = async (
  bookId: number,
  fileId: number,
  progress: ReaderProgress,
): Promise<Awaited<ReturnType<typeof v1Api.setReadingProgress>>> => {
  const clientUpdatedAt = progress.updatedAt ?? new Date().toISOString();
  return v1Api.setReadingProgress(bookId, progress.progression, false, {
    fileId,
    locator: { value: progress.value, progression: progress.progression },
    clientUpdatedAt,
    completed: progress.completed,
  });
};

export const readerFileFromV1 = (input: {
  bookId: number;
  fileId: number;
  title: string;
  author?: string | null;
  format: string;
  etag?: string | null;
}): ReaderFile => ({
  ...input,
  contentUrl: readerContentUrl(input.bookId, input.fileId),
});
