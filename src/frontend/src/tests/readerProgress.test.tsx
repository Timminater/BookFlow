import { describe, expect, it } from 'vitest';

import { formatReaderProgress } from '../reader/readerProgress';

describe('formatReaderProgress', () => {
  it('formats exact fixed-layout page data alongside the percentage', () => {
    expect(formatReaderProgress({ progression: 0.18, currentPage: 36, totalPages: 200 })).toBe(
      '36/200 (18%)',
    );
  });

  it('handles the first and final page plus zero and complete progress', () => {
    expect(formatReaderProgress({ progression: 0, currentPage: 1, totalPages: 200 })).toBe(
      '1/200 (0%)',
    );
    expect(formatReaderProgress({ progression: 1, currentPage: 200, totalPages: 200 })).toBe(
      '200/200 (100%)',
    );
  });

  it('clamps progression and a current page beyond the reliable total', () => {
    expect(formatReaderProgress({ progression: -2, currentPage: 0, totalPages: 200 })).toBe('0%');
    expect(formatReaderProgress({ progression: 2, currentPage: 201, totalPages: 200 })).toBe(
      '200/200 (100%)',
    );
  });

  it('uses percentage-only fallback for missing, zero or invalid page data', () => {
    expect(formatReaderProgress({ progression: 0.18 })).toBe('18%');
    expect(formatReaderProgress({ progression: 0.18, currentPage: 36, totalPages: 0 })).toBe('18%');
    expect(
      formatReaderProgress({
        progression: Number.NaN,
        currentPage: Number.NaN,
        totalPages: Number.POSITIVE_INFINITY,
      }),
    ).toBe('0%');
  });
});
