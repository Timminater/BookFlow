import type { ReaderLocator } from './types';

const clampedProgression = (progression: number): number => {
  if (!Number.isFinite(progression)) return 0;
  return Math.max(0, Math.min(1, progression));
};

const positiveInteger = (value: number | undefined): number | null =>
  Number.isInteger(value) && Number(value) > 0 ? Number(value) : null;

/** Format reliable fixed-page data while retaining percentage-only fallback for reflowable books. */
export const formatReaderProgress = (
  position: Pick<ReaderLocator, 'progression' | 'currentPage' | 'totalPages'>,
): string => {
  const percentage = `${Math.round(clampedProgression(position.progression) * 100)}%`;
  const totalPages = positiveInteger(position.totalPages);
  const currentPage = positiveInteger(position.currentPage);
  if (totalPages === null || currentPage === null) return percentage;
  return `${Math.min(currentPage, totalPages)}/${totalPages} (${percentage})`;
};
