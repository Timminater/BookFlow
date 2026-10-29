import type { ReaderLocator } from './types';

const locatorKey = (bookId: number, fileId: number): string =>
  `bookflow-reader-locator-v1:${bookId}:${fileId}`;

export const loadReaderLocator = (bookId: number, fileId: number): ReaderLocator | null => {
  try {
    const raw = window.localStorage.getItem(locatorKey(bookId, fileId));
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (
      !value ||
      typeof value !== 'object' ||
      !('value' in value) ||
      !('progression' in value) ||
      typeof value.value !== 'string' ||
      typeof value.progression !== 'number'
    ) {
      return null;
    }
    return {
      value: value.value,
      progression: Math.max(0, Math.min(1, value.progression)),
      ...('updatedAt' in value && typeof value.updatedAt === 'string'
        ? { updatedAt: value.updatedAt }
        : {}),
    };
  } catch {
    return null;
  }
};

export const saveReaderLocator = (bookId: number, fileId: number, locator: ReaderLocator): void => {
  try {
    window.localStorage.setItem(
      locatorKey(bookId, fileId),
      JSON.stringify({ ...locator, updatedAt: locator.updatedAt ?? new Date().toISOString() }),
    );
  } catch {
    // Exact position is a convenience. The server-side percentage still syncs.
  }
};
