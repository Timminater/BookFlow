import type { V1Candidate } from '../types/v1';

export type InteractiveSearchSort = 'score' | 'title' | 'seeders' | 'size';
export type InteractiveDecision = 'eligible' | 'rejected' | 'blocked';

export interface InteractiveCandidate {
  candidate: V1Candidate;
  id: number;
  title: string;
  source: string;
  indexer: string | null;
  protocol: string;
  language: string;
  format: string;
  size: string;
  seeders: number;
  peers: number;
  score: number;
  decision: InteractiveDecision;
  rejectReasons: string[];
  selected: boolean;
}

export interface InteractiveCandidateFallbacks {
  unknown: string;
  unknownSource: string;
}

export const formatBytes = (bytes: number | null | undefined, unknownFallback = ''): string => {
  if (!bytes || bytes < 1) return unknownFallback;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index < 2 ? 0 : 1)} ${units[index]}`;
};

export const toInteractiveCandidate = (
  candidate: V1Candidate,
  fallbacks: InteractiveCandidateFallbacks,
): InteractiveCandidate => {
  let decision: InteractiveDecision = 'rejected';
  if (candidate.blocked === 1) decision = 'blocked';
  else if (candidate.accepted === 1) decision = 'eligible';
  return {
    candidate,
    id: candidate.id,
    title: candidate.title?.trim() || `#${candidate.id}`,
    source: candidate.source?.trim() || fallbacks.unknownSource,
    indexer: candidate.indexer?.trim() || null,
    protocol: candidate.protocol?.trim() || fallbacks.unknown,
    language: candidate.language?.trim() || fallbacks.unknown,
    format: candidate.file_format?.trim().toUpperCase() || fallbacks.unknown,
    size: formatBytes(candidate.size_bytes, fallbacks.unknown),
    seeders: candidate.seeders ?? 0,
    peers: candidate.peers ?? 0,
    score: candidate.total_score,
    decision,
    rejectReasons: candidate.reject_reasons ?? [],
    selected: candidate.selected_by_admin === 1,
  };
};

export const filterInteractiveCandidates = (
  candidates: InteractiveCandidate[],
  source: string,
  format: string,
  language: string,
): InteractiveCandidate[] =>
  candidates.filter(
    (candidate) =>
      (source === 'all' || candidate.source === source) &&
      (format === 'all' || candidate.format === format) &&
      (language === 'all' || candidate.language === language),
  );

export const sortInteractiveCandidates = (
  candidates: V1Candidate[],
  sort: InteractiveSearchSort,
): V1Candidate[] =>
  candidates
    .map((candidate, index) => ({ candidate, index }))
    .toSorted((left, right) => {
      let result = 0;
      if (sort === 'title')
        result = (left.candidate.title ?? '').localeCompare(right.candidate.title ?? '');
      else if (sort === 'seeders')
        result = (right.candidate.seeders ?? 0) - (left.candidate.seeders ?? 0);
      else if (sort === 'size')
        result = (right.candidate.size_bytes ?? 0) - (left.candidate.size_bytes ?? 0);
      else result = right.candidate.total_score - left.candidate.total_score;
      return result || left.index - right.index;
    })
    .map(({ candidate }) => candidate);
