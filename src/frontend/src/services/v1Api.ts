import type {
  V1RequestProcess,
  V1RequestSearchRun,
  V1AdminRequest,
  V1Attempt,
  V1Book,
  V1BookDetail,
  V1BulkLibraryResponse,
  V1Backup,
  V1BestsellerAdminStatus,
  V1CatalogBook,
  V1CatalogBookDetail,
  V1CatalogEntity,
  V1Candidate,
  V1ErrorEnvelope,
  V1Page,
  V1Request,
  V1KoboSettings,
  V1MaintenanceStatus,
  V1MetadataReport,
  V1MetadataPreview,
  V1MetadataRevision,
  V1Monitor,
  V1MonitorCreate,
  V1MonitorDiscovery,
  V1MonitorUpdate,
  V1PushSubscription,
  V1PushRule,
  V1PatcherManifest,
  V1ReadingProgress,
  V1SearchBooksResponse,
  V1ReleaseProfile,
  V1ReleaseProfileConfig,
  V1ReleaseProfilePreview,
  V1SuggestionPage,
  V1SuggestionsResponse,
  V1UserRequest,
} from '../types/v1';
import { getApiBase } from '../utils/basePath';

export interface V1CatalogDeletionPlan {
  canonical_book_id: number;
  equivalent_book_ids: number[];
  book: { id: number; title: string; author: string | null };
  files: Array<{
    id: number;
    file_format: string;
    storage_path: string;
    derived_from_file_id: number | null;
  }>;
  counts: Record<
    | 'memberships'
    | 'entitlements'
    | 'reading_progress'
    | 'requests'
    | 'active_processes'
    | 'active_downloads',
    number
  >;
}

export interface V1CatalogDeletionOperation {
  id: string;
  state: string;
  pending: number;
  plan: V1CatalogDeletionPlan;
  cleanup: Array<{ storage_path: string; state: string; attempts: number; error: string | null }>;
}

const V1_BASE = `${getApiBase()}/v1`;
const MUTATION_TIMEOUT_MS = 15_000;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const parseError = (value: unknown): V1ErrorEnvelope['error'] => {
  const candidate = isRecord(value) && isRecord(value.error) ? value.error : {};
  return {
    code: typeof candidate.code === 'string' ? candidate.code : 'unexpected_response',
    message: typeof candidate.message === 'string' ? candidate.message : 'The request failed',
    details: candidate.details,
    correlation_id: typeof candidate.correlation_id === 'string' ? candidate.correlation_id : '',
  };
};

export class V1ApiError extends Error {
  readonly code: string;
  readonly correlationId: string;

  constructor(payload: V1ErrorEnvelope['error']) {
    super(payload.message);
    this.name = 'V1ApiError';
    this.code = payload.code;
    this.correlationId = payload.correlation_id;
  }
}

export interface IssueReportResponse {
  issue_number: number;
  attachment_status: 'none' | 'attached' | 'failed';
  warning?: string;
}

const isIssueReportResponse = (value: unknown): value is IssueReportResponse => {
  if (!isRecord(value) || typeof value.issue_number !== 'number') return false;
  return (
    value.attachment_status === 'none' ||
    value.attachment_status === 'attached' ||
    value.attachment_status === 'failed'
  );
};

/**
 * Submit an issue report without serializing FormData or setting a Content-Type.
 * The browser supplies the multipart boundary, while this keeps session cookies,
 * CSRF protection and the retry idempotency key on the request.
 */
export const submitIssueReport = async (
  data: FormData,
  idempotencyKey: string,
): Promise<IssueReportResponse> => {
  const response = await fetch(`${V1_BASE}/issue-reports`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      'X-BookFlow-CSRF': '1',
      'Idempotency-Key': idempotencyKey,
    },
    body: data,
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new V1ApiError(parseError(body));
  }
  if (!isIssueReportResponse(body)) {
    throw new V1ApiError({
      code: 'unexpected_response',
      message: 'The server returned an unexpected issue-report response',
      details: null,
      correlation_id: '',
    });
  }
  return body;
};

const requestV1 = async <T>(path: string, init: RequestInit = {}): Promise<T> => {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (method !== 'GET' && method !== 'HEAD') {
    headers.set('X-BookFlow-CSRF', '1');
  }
  const response = await fetch(`${V1_BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers,
  });
  const body: unknown = await response.json();
  if (!response.ok) {
    throw new V1ApiError(parseError(body));
  }
  // Fetch cannot validate an OpenAPI response at runtime; endpoint-specific
  // TypeScript types form the checked boundary for successful JSON payloads.
  // oxlint-disable-next-line typescript/no-unsafe-type-assertion
  return body as T;
};

const pageQuery = (offset = 0, limit = 50): string => `?offset=${offset}&limit=${limit}`;

export const v1Api = {
  searchBooks: (query: string, signal?: AbortSignal): Promise<V1SearchBooksResponse> =>
    requestV1(`/search/books?q=${encodeURIComponent(query)}&limit=8`, { signal }),
  catalog: (
    options: {
      q?: string;
      format?: string;
      sort?: string;
      authorId?: string;
      seriesId?: string;
      offset?: number;
      limit?: number;
    } = {},
  ): Promise<V1Page<V1CatalogBook>> => {
    const query = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 30),
    });
    if (options.q) query.set('q', options.q);
    if (options.format) query.set('format', options.format);
    if (options.sort) query.set('sort', options.sort);
    if (options.authorId) query.set('author_id', options.authorId);
    if (options.seriesId) query.set('series_id', options.seriesId);
    return requestV1(`/catalog/books?${query.toString()}`);
  },
  recentCatalog: (): Promise<V1Page<V1CatalogBook>> => requestV1('/catalog/recent?limit=12'),
  userRequests: (offset = 0, limit = 10): Promise<V1Page<V1UserRequest>> =>
    requestV1(`/users/me/requests${pageQuery(offset, limit)}`),
  suggestions: (): Promise<V1SuggestionsResponse> => requestV1('/suggestions'),
  suggestionList: (listKey: string, offset = 0, limit = 30): Promise<V1SuggestionPage> =>
    requestV1(
      `/suggestions/${encodeURIComponent(listKey)}?offset=${Math.max(0, offset)}&limit=${Math.max(1, limit)}`,
    ),
  catalogBook: (bookId: number): Promise<V1CatalogBookDetail> =>
    requestV1(`/catalog/books/${bookId}`),
  catalogBookByIsbn: (isbn: string): Promise<V1CatalogBookDetail> =>
    requestV1(`/catalog/books/isbn/${encodeURIComponent(isbn)}`),
  catalogDeletionPlan: (
    bookId: number,
  ): Promise<{ plan: V1CatalogDeletionPlan; plan_token: string }> =>
    requestV1(`/admin/catalog/books/${bookId}`),
  deleteCatalogBook: (
    bookId: number,
    planToken: string,
  ): Promise<{
    status: string;
    operation_id?: string;
    cleanup: { failed: unknown[]; pending: number };
  }> =>
    requestV1(`/admin/catalog/books/${bookId}`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan_token: planToken }),
    }),
  retryCatalogDeletionCleanup: (
    operationId: string,
  ): Promise<{ pending: number; failed: unknown[] }> =>
    requestV1(`/admin/catalog/deletions/${encodeURIComponent(operationId)}/retry-cleanup`, {
      method: 'POST',
    }),
  catalogDeletionStatus: (operationId: string): Promise<V1CatalogDeletionOperation> =>
    requestV1(`/admin/catalog/deletions/${encodeURIComponent(operationId)}`),
  bulkAddLibraryBooks: (bookIds: number[]): Promise<V1BulkLibraryResponse> =>
    requestV1('/library/books/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book_ids: bookIds }),
    }),
  catalogPresence: (
    candidates: Array<{ provider?: string; provider_book_id?: string; isbn?: string }>,
  ): Promise<{ present: number[] }> =>
    requestV1('/catalog/presence', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ candidates }),
    }),
  catalogEntities: (
    kind: 'authors' | 'series',
    options: { q?: string; offset?: number; limit?: number } = {},
  ): Promise<V1Page<V1CatalogEntity>> => {
    const query = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 30),
    });
    if (options.q) query.set('q', options.q);
    return requestV1(`/catalog/${kind}?${query.toString()}`);
  },
  readingProgress: (): Promise<V1Page<V1ReadingProgress>> =>
    requestV1('/users/me/reading-progress?active=true'),
  libraryFileDownloadUrl: (bookId: number, fileId: number): string =>
    `${V1_BASE}/library/books/${bookId}/files/${fileId}/download`,
  readingProgressForBook: (bookId: number): Promise<{ progress: V1ReadingProgress }> =>
    requestV1(`/users/me/reading-progress/${bookId}`),
  setReadingProgress: (
    bookId: number,
    progress: number,
    reset = false,
    reader?: {
      fileId: number;
      locator: unknown;
      clientUpdatedAt: string;
      completed: boolean;
    },
  ): Promise<{ progress: V1ReadingProgress }> =>
    requestV1(`/users/me/reading-progress/${bookId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...(reader
          ? {
              progression: progress,
              file_id: reader.fileId,
              locator: reader.locator,
              client_updated_at: reader.clientUpdatedAt,
              completed: reader.completed,
            }
          : { progress }),
        reset,
      }),
    }),
  clearReadingProgress: (bookId: number): Promise<{ book_id: number; reset: true }> =>
    requestV1(`/users/me/reading-progress/${bookId}`, { method: 'DELETE' }),
  monitors: (): Promise<V1Page<V1Monitor>> => requestV1('/monitors?limit=100'),
  createMonitor: (body: V1MonitorCreate): Promise<{ monitor: V1Monitor }> =>
    requestV1('/monitors', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateMonitor: (id: number, body: V1MonitorUpdate): Promise<{ monitor: V1Monitor }> =>
    requestV1(`/monitors/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  scanMonitor: (id: number): Promise<{ scheduled: boolean }> =>
    requestV1(`/monitors/${id}/scan`, { method: 'POST' }),
  monitorDiscoveries: (id: number): Promise<V1Page<V1MonitorDiscovery>> =>
    requestV1(`/monitors/${id}/discoveries?limit=100`),
  updateMonitorDiscovery: (
    monitorId: number,
    discoveryId: number,
    state: Extract<V1MonitorDiscovery['state'], 'discovered' | 'ignored'>,
  ): Promise<{ discovery: V1MonitorDiscovery }> =>
    requestV1(`/monitors/${monitorId}/discoveries/${discoveryId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state }),
    }),
  deleteMonitor: (id: number): Promise<void> => requestV1(`/monitors/${id}`, { method: 'DELETE' }),
  pushSubscriptions: (): Promise<V1Page<V1PushSubscription>> =>
    requestV1('/users/me/push/subscriptions'),
  savePushSubscription: (subscription: PushSubscriptionJSON, deviceLabel?: string): Promise<void> =>
    requestV1('/users/me/push/subscriptions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...subscription, device_label: deviceLabel }),
    }),
  deletePushSubscription: (id: number): Promise<void> =>
    requestV1(`/users/me/push/subscriptions/${id}`, { method: 'DELETE' }),
  vapidKey: (): Promise<{ configured: boolean; public_key: string | null }> =>
    requestV1('/users/me/push/vapid-key'),
  pushRules: (): Promise<V1Page<V1PushRule>> => requestV1('/users/me/push/rules'),
  setPushRule: (
    eventType: string,
    enabled: boolean,
    filters: Record<string, unknown> = {},
  ): Promise<V1PushRule> =>
    requestV1(`/users/me/push/rules/${encodeURIComponent(eventType)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled, filters }),
    }),
  testPush: (): Promise<{ sent: number }> => requestV1('/users/me/push/test', { method: 'POST' }),
  metadataReports: (): Promise<V1Page<V1MetadataReport>> =>
    requestV1('/metadata-reports?limit=100'),
  createMetadataReport: (
    bookId: number,
    fieldName: string,
    note?: string,
  ): Promise<V1MetadataReport> =>
    requestV1(`/catalog/books/${bookId}/metadata-reports`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields: [fieldName], explanation: note ?? '' }),
    }),
  adminMetadataReports: (): Promise<V1Page<V1MetadataReport>> =>
    requestV1('/admin/metadata-reports?limit=100'),
  previewMetadata: (bookId: number, changes: Record<string, unknown>): Promise<V1MetadataPreview> =>
    requestV1(`/admin/books/${bookId}/metadata/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ changes }),
    }),
  applyMetadata: (
    bookId: number,
    changes: Record<string, unknown>,
    reason: string,
    reportId?: number,
  ): Promise<V1MetadataPreview & { revision_id: number }> =>
    requestV1(`/admin/books/${bookId}/metadata/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ changes, reason, report_id: reportId }),
    }),
  rejectMetadataReport: (id: number, note: string): Promise<void> =>
    requestV1(`/admin/metadata-reports/${id}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note }),
    }),
  metadataHistory: (bookId: number): Promise<V1Page<V1MetadataRevision>> =>
    requestV1(`/admin/books/${bookId}/metadata/history`),
  revertMetadata: (
    bookId: number,
    revisionId: number,
    reason = `Restored revision ${revisionId}`,
  ): Promise<V1MetadataPreview> =>
    requestV1(`/admin/books/${bookId}/metadata/revert/${revisionId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    }),
  backups: (): Promise<V1Page<V1Backup>> => requestV1('/admin/backups?limit=100'),
  createBackup: (): Promise<V1Backup> => requestV1('/admin/backups', { method: 'POST' }),
  verifyBackup: (id: string): Promise<V1Backup> =>
    requestV1(`/admin/backups/${encodeURIComponent(id)}/verify`, { method: 'POST' }),
  deleteBackup: (id: string): Promise<void> =>
    requestV1(`/admin/backups/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  stageRestore: (id: string): Promise<{ staged: boolean; restart_required: boolean }> =>
    requestV1(`/admin/backups/${encodeURIComponent(id)}/stage-restore`, { method: 'POST' }),
  backupDownloadUrl: (id: string): string =>
    `${V1_BASE}/admin/backups/${encodeURIComponent(id)}/download`,
  createKoboPairingCode: (): Promise<{ code: string; expires_at: string }> =>
    requestV1('/users/me/kobo-pairing-code', { method: 'POST' }),
  patcherManifest: (): Promise<V1PatcherManifest> => requestV1('/kobo/patcher/manifest'),
  patcherDownloadUrl: (): string => `${V1_BASE}/kobo/patcher/download`,
  library: (
    options: {
      q?: string;
      file_state?: 'all' | 'with-files' | 'needs-files';
      format?: string;
      sort?: 'recent' | 'title' | 'author' | 'year';
      shelf?: 'kobo';
      offset?: number;
      limit?: number;
    } = {},
  ): Promise<V1Page<V1Book>> => {
    const query = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 30),
    });
    if (options.q) query.set('q', options.q);
    if (options.file_state && options.file_state !== 'all') {
      query.set('file_state', options.file_state);
    }
    if (options.format) query.set('format', options.format);
    if (options.sort && options.sort !== 'recent') query.set('sort', options.sort);
    if (options.shelf) query.set('shelf', options.shelf);
    return requestV1(`/library/books?${query.toString()}`);
  },
  book: (bookId: number): Promise<V1BookDetail> => requestV1(`/library/books/${bookId}`),
  createRequest: (
    body: {
      book_id: number;
      /** Every request selects one active named release profile. */
      profile_id: number;
      content_type?: 'ebook';
      note?: string;
      add_to_kobo_when_available?: boolean;
    },
    idempotencyKey: string,
  ): Promise<{ request: V1Request }> =>
    requestV1('/requests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(MUTATION_TIMEOUT_MS),
    }),
  cancelRequest: (requestId: number, idempotencyKey: string): Promise<{ request: V1Request }> =>
    requestV1(`/requests/${requestId}/cancel`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  cancelRequestProcess: (
    requestProcessId: number,
  ): Promise<{
    request_process_id: number;
    status: 'cancelled';
    request_ids: number[];
  }> =>
    requestV1(`/admin/request-processes/${requestProcessId}/cancel`, {
      method: 'POST',
    }),
  requestProcesses: (offset?: number, limit?: number): Promise<V1Page<V1RequestProcess>> =>
    requestV1(`/admin/request-processes${pageQuery(offset, limit)}`),
  adminRequests: (
    status?: V1Request['status'],
    offset?: number,
    limit?: number,
  ): Promise<V1Page<V1AdminRequest>> => {
    const query = new URLSearchParams({
      offset: String(offset ?? 0),
      limit: String(limit ?? 50),
    });
    if (status) query.set('status', status);
    return requestV1(`/admin/requests?${query.toString()}`);
  },
  approveRequest: (
    requestId: number,
  ): Promise<{ request: V1Request; request_process: V1RequestProcess | null }> =>
    requestV1(`/admin/requests/${requestId}/approve`, { method: 'POST' }),
  rejectRequest: (requestId: number, adminNote?: string): Promise<{ status: 'rejected' }> =>
    requestV1(`/admin/requests/${requestId}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ admin_note: adminNote }),
    }),
  candidates: (requestProcessId: number, searchRunId?: number): Promise<V1Page<V1Candidate>> => {
    const query = new URLSearchParams({ offset: '0', limit: '100' });
    if (searchRunId) query.set('search_run_id', String(searchRunId));
    return requestV1(`/admin/request-processes/${requestProcessId}/candidates?${query.toString()}`);
  },
  attempts: (requestProcessId: number): Promise<V1Page<V1Attempt>> =>
    requestV1(`/admin/request-processes/${requestProcessId}/attempts${pageQuery()}`),
  maintenance: (): Promise<V1MaintenanceStatus> => requestV1('/admin/maintenance'),
  bestsellerStatus: (): Promise<V1BestsellerAdminStatus> => requestV1('/admin/bestseller/status'),
  syncBestseller: (): Promise<{ scheduled: boolean; job_id: number; job_key: string }> =>
    requestV1('/admin/bestseller/sync', { method: 'POST' }),
  searchNow: (
    requestProcessId: number,
  ): Promise<{ request_process_id: number; scheduled: boolean }> =>
    requestV1(`/admin/request-processes/${requestProcessId}/search-now`, { method: 'POST' }),
  setMonitoring: (
    requestProcessId: number,
    enabled: boolean,
  ): Promise<{ monitoring_enabled: boolean }> =>
    requestV1(`/admin/request-processes/${requestProcessId}/monitor`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }),
  releaseProfiles: (): Promise<V1Page<V1ReleaseProfile>> =>
    requestV1('/release-profiles?limit=100'),
  adminReleaseProfiles: (): Promise<V1Page<V1ReleaseProfile>> =>
    requestV1('/admin/release-profiles?limit=100'),
  createReleaseProfile: (body: {
    name: string;
    is_default: boolean;
    is_active: boolean;
    config: V1ReleaseProfileConfig;
  }): Promise<{ profile: V1ReleaseProfile }> =>
    requestV1('/admin/release-profiles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateReleaseProfile: (
    id: number,
    body: Partial<{
      name: string;
      is_default: boolean;
      is_active: boolean;
      config: V1ReleaseProfileConfig;
    }>,
  ): Promise<{ profile: V1ReleaseProfile }> =>
    requestV1(`/admin/release-profiles/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteReleaseProfile: (id: number): Promise<void> =>
    requestV1(`/admin/release-profiles/${id}`, { method: 'DELETE' }),
  previewReleaseProfile: (
    config: V1ReleaseProfileConfig,
    candidate: Record<string, unknown>,
  ): Promise<V1ReleaseProfilePreview> =>
    requestV1('/admin/release-profiles/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config, candidate }),
    }),
  interactiveSearch: (
    requestProcessId: number,
  ): Promise<{
    request_process_id: number;
    search_run: V1RequestSearchRun;
    scheduled: boolean;
    interactive: true;
  }> =>
    requestV1(`/admin/request-processes/${requestProcessId}/interactive-search`, {
      method: 'POST',
    }),
  requestSearchRun: (
    requestProcessId: number,
    searchRunId: number,
  ): Promise<{ search_run: V1RequestSearchRun }> =>
    requestV1(`/admin/request-processes/${requestProcessId}/search-runs/${searchRunId}`),
  selectCandidate: (
    candidateId: number,
    force = false,
    reason?: string,
  ): Promise<{ candidate_id: number; selected: boolean; forced: boolean }> =>
    requestV1(`/admin/candidates/${candidateId}/select`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force, reason }),
    }),
  blockCandidate: (candidateId: number): Promise<{ blocked: boolean }> =>
    requestV1(`/admin/candidates/${candidateId}/block`, { method: 'POST' }),
  koboSettings: (): Promise<V1KoboSettings> => requestV1('/users/me/kobo-settings'),
  setKoboEnabled: (enabled: boolean): Promise<{ enabled: boolean }> =>
    requestV1('/users/me/kobo-settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }),
  issueKoboToken: (): Promise<{
    token_id: number;
    token: string;
    api_endpoint?: string;
    sync_url: string;
    shown_once: true;
  }> => requestV1('/users/me/kobo-token', { method: 'POST' }),
  rotateKoboToken: (
    tokenId: number,
  ): Promise<{
    token_id: number;
    token: string;
    api_endpoint?: string;
    sync_url: string;
    shown_once: true;
  }> => requestV1(`/users/me/kobo-token/${tokenId}/rotate`, { method: 'POST' }),
  revokeKoboToken: (tokenId: number): Promise<{ token_id: number; revoked: true }> =>
    requestV1(`/users/me/kobo-token/${tokenId}`, { method: 'DELETE' }),
  addKoboBook: (
    bookId: number,
    fileId: number,
    preferredFormat?: string,
  ): Promise<{ revision: number }> =>
    requestV1(`/users/me/kobo-books/${bookId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: fileId, preferred_format: preferredFormat }),
    }),
  removeKoboBook: (bookId: number): Promise<{ revision: number }> =>
    requestV1(`/users/me/kobo-books/${bookId}`, { method: 'DELETE' }),
};
