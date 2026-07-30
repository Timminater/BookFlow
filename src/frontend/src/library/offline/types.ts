/**
 * Types shared by the offline reader cache. `accountId` must be a stable,
 * non-secret user identifier (for example the authenticated user id), never a
 * session cookie or bearer token.
 */
export interface OfflineBookMetadata {
  accountId: string;
  bookId: number;
  /** Stable file/history id supplied by the library API. */
  fileId: number;
  title: string;
  author: string | null;
  format: string;
  coverUrl: string | null;
  contentUrl: string;
}

export interface ReadingProgress {
  locator: string;
  progression: number;
  completed: boolean;
  /** ISO-8601 timestamp of the reading action, not the sync time. */
  lastReadAt: string;
}

export interface OfflineBook extends OfflineBookMetadata {
  content: Blob;
  cover: Blob | null;
  etag: string | null;
  contentType: string | null;
  /** Used to select the three entries in Continue Reading. */
  lastAccessedAt: string;
  progress: ReadingProgress;
}

export interface PendingProgress extends ReadingProgress {
  accountId: string;
  bookId: number;
  fileId: number;
}

export interface CacheBookInput extends OfflineBookMetadata {
  content: Blob;
  cover?: Blob | null;
  etag?: string | null;
  contentType?: string | null;
  progress?: ReadingProgress;
}

export interface CacheFetchInput extends OfflineBookMetadata {
  cover?: Blob | null;
  progress?: ReadingProgress;
}

export interface ProgressUpdate {
  locator: string;
  progression: number;
  completed: boolean;
  lastReadAt?: string;
}

export interface CacheReconciliation {
  /** Offline file ids removed in order to keep the cache to three active books. */
  evictedFileIds: number[];
  /** Eviction was deferred because the readers are still open. */
  deferredFileIds: number[];
}

export interface RefreshResult {
  changed: boolean;
  etag: string | null;
}

/** Safe, stable diagnostics for a single Continue Reading cache candidate. */
export type OfflineCacheErrorCode =
  | 'detail_unavailable'
  | 'not_entitled'
  | 'file_missing'
  | 'unsupported_format'
  | 'download_failed'
  | 'storage_quota'
  | 'indexeddb_failed';

export type OfflineCacheOutcomeStatus =
  | 'cached'
  | 'temporarily_failed'
  | 'permanently_ineligible'
  | 'kept_existing_copy';

export interface OfflineCacheOutcome {
  bookId: number;
  fileId: number | null;
  title: string | null;
  status: OfflineCacheOutcomeStatus;
  errorCode: OfflineCacheErrorCode | null;
}

export interface ProgressSyncResponse {
  /** The authoritative state returned by the server, when it has one. */
  progress: ReadingProgress | null;
}

export type ProgressSender = (update: PendingProgress) => Promise<ProgressSyncResponse>;

export interface ProgressSyncSummary {
  sent: number;
  discardedAsStale: number;
  failed: number;
}
