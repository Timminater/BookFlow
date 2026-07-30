import { describe, expect, it } from 'vitest';

import {
  filterInteractiveCandidates,
  formatBytes,
  sortInteractiveCandidates,
  toInteractiveCandidate,
} from '../pages/interactiveSearchModel';
import type { V1Candidate } from '../types/v1';

const candidate = (id: number, values: Partial<V1Candidate>): V1Candidate => ({
  id,
  request_process_id: 1,
  accepted: 1,
  total_score: 0,
  file_format: 'epub',
  ...values,
});
const fallbacks = { unknown: 'Unknown', unknownSource: 'Unknown source' };

describe('interactive search result presentation', () => {
  it('sorts all candidates including rejected candidates by profile score', () => {
    const rejected = candidate(1, { total_score: -20, accepted: 0 });
    const eligible = candidate(2, { total_score: 30 });
    expect(sortInteractiveCandidates([rejected, eligible], 'score').map((item) => item.id)).toEqual(
      [2, 1],
    );
  });

  it('formats known and unknown sizes for scanable result rows', () => {
    expect(formatBytes(1024 * 1024 * 2)).toBe('2.0 MB');
    expect(formatBytes(null, 'Unknown')).toBe('Unknown');
  });

  it('adapts all candidate metadata and server decisions', () => {
    const adapted = toInteractiveCandidate(
      candidate(7, {
        title: 'Release title',
        source: 'Prowlarr',
        indexer: 'Indexer',
        protocol: 'torrent',
        language: 'nl',
        file_format: 'epub',
        size_bytes: 1024,
        seeders: 8,
        peers: 3,
        total_score: 42,
        accepted: 0,
        blocked: 1,
        reject_reasons: ['Blocked hash', 'Wrong language'],
        selected_by_admin: 1,
      }),
      fallbacks,
    );
    expect(adapted).toMatchObject({
      id: 7,
      title: 'Release title',
      source: 'Prowlarr',
      indexer: 'Indexer',
      protocol: 'torrent',
      language: 'nl',
      format: 'EPUB',
      size: '1 KB',
      seeders: 8,
      peers: 3,
      score: 42,
      decision: 'blocked',
      rejectReasons: ['Blocked hash', 'Wrong language'],
      selected: true,
    });
  });

  it('uses safe fallbacks for missing optional candidate metadata', () => {
    expect(toInteractiveCandidate(candidate(9, { file_format: null }), fallbacks)).toMatchObject({
      title: '#9',
      source: 'Unknown source',
      indexer: null,
      protocol: 'Unknown',
      language: 'Unknown',
      format: 'Unknown',
      size: 'Unknown',
      decision: 'eligible',
    });
  });

  it('filters by source, format and language without mutating input', () => {
    const originals = [
      toInteractiveCandidate(candidate(1, { source: 'Prowlarr', language: 'nl' }), fallbacks),
      toInteractiveCandidate(
        candidate(2, { source: 'Direct', language: 'en', file_format: 'pdf' }),
        fallbacks,
      ),
    ];
    const snapshot = [...originals];
    expect(
      filterInteractiveCandidates(originals, 'Direct', 'PDF', 'en').map(({ id }) => id),
    ).toEqual([2]);
    expect(originals).toEqual(snapshot);
  });

  it.each([
    ['score', [2, 1]],
    ['seeders', [2, 1]],
    ['size', [2, 1]],
    ['title', [1, 2]],
  ] as const)('sorts stably by %s', (sort, ids) => {
    const values = [
      candidate(1, { title: 'Alpha', total_score: 1, seeders: 1, size_bytes: 1 }),
      candidate(2, { title: 'Beta', total_score: 2, seeders: 2, size_bytes: 2 }),
    ];
    expect(sortInteractiveCandidates(values, sort).map(({ id }) => id)).toEqual(ids);
  });
});
