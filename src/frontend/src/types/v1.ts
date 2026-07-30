export interface V1ErrorEnvelope {
  error: {
    code: string;
    message: string;
    details: unknown;
    correlation_id: string;
  };
}

export interface V1Page<T> {
  items: T[];
  offset: number;
  limit: number;
  total?: number;
}

export interface V1ReadingProgress {
  book_id: number;
  progress: number;
  progression?: number;
  active: 0 | 1;
  completed?: boolean;
  file_id?: number | null;
  locator?: unknown;
  reader_updated_at?: string | null;
  source: string;
  revision: number;
  updated_at: string;
  title?: string;
  author?: string | null;
  cover_url?: string | null;
}

export interface V1CatalogBook {
  id: number;
  in_library?: boolean;
  metadata_provider?: string;
  provider_book_id?: string;
  title: string;
  author: string | null;
  language?: string | null;
  publish_year?: number | null;
  cover_url?: string | null;
  formats?: string[];
  subtitle?: string | null;
  series_name?: string | null;
  series_position?: string | null;
  isbn?: string | null;
  isbn_13?: string | null;
  description?: string | null;
  added_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}
export interface V1SearchBook {
  id: number;
  title: string;
  author: string | null;
  cover_url: string | null;
  publish_year: number | null;
  isbn_13: string | null;
  catalog_visible: boolean;
  has_imported_file: boolean;
  in_library: boolean;
  requested: boolean;
  is_trending: boolean;
}
export interface V1SearchBooksResponse {
  items: V1SearchBook[];
  limit: number;
}

export interface V1SuggestionBook extends V1CatalogBook {
  in_library: boolean;
  request_status: string | null;
}

export type V1BulkLibraryStatus = 'added' | 'already_in_library' | 'unavailable' | 'not_found';

export interface V1BulkLibraryResult {
  book_id: number;
  canonical_book_id?: number;
  status: V1BulkLibraryStatus;
}

export interface V1BulkLibraryResponse {
  results: V1BulkLibraryResult[];
  summary: {
    submitted: number;
    added: number;
    already_in_library: number;
    failed: number;
  };
}

export interface V1UserRequest {
  id: number;
  book_id: number;
  title: string;
  author: string | null;
  cover_url: string | null;
  content_type: 'ebook';
  status: string;
  delivery_state: string | null;
  created_at: string;
  updated_at: string;
  profile_id?: number | null;
  profile_name?: string | null;
}

export interface V1SuggestionSnapshot {
  year: number;
  week: number;
  fetched_at: string;
}

export interface V1SuggestionList {
  key: string;
  label: string;
  total: number;
  items: V1SuggestionBook[];
}

export interface V1SuggestionsResponse {
  snapshot: V1SuggestionSnapshot | null;
  lists: V1SuggestionList[];
}

export type V1SuggestionPage = V1Page<V1SuggestionBook> & {
  label?: string;
  snapshot?: V1SuggestionSnapshot | null;
};

export interface V1CatalogBookDetail {
  book: V1CatalogBook & { metadata?: Record<string, unknown> };
  in_library: boolean;
  entitled: boolean;
  delivery_state?: 'available' | null;
  available_formats: string[];
  request: {
    id: number;
    status: V1Request['status'];
    add_to_kobo_when_available: 0 | 1;
    created_at: string;
    updated_at: string;
  } | null;
  personal_status: {
    library_added_at: string | null;
    reading_progress: {
      progress: number;
      active: 0 | 1;
      source: string;
      updated_at: string;
    } | null;
    available_formats: string[];
    file_count: number;
    kobo_state: 'active' | 'deleted' | null;
  };
}

export interface V1CatalogEntity {
  id: number;
  name: string;
  book_count: number;
}

export interface V1Monitor {
  id: number;
  type: 'author' | 'series';
  query: string;
  provider: string | null;
  provider_id: string | null;
  /**
   * Provider-specific search data is optional until every adapter exposes a
   * structured profile.  Keep it lossless so newer servers do not require a
   * frontend release before their monitors can be displayed.
   */
  search_profile?: Record<string, unknown> | null;
  action: 'notify' | 'request' | 'auto_acquire';
  add_to_kobo: boolean;
  status: 'active' | 'paused';
  scan_interval_seconds?: number;
  next_scan_at: string | null;
  created_at: string;
}

export interface V1MonitorCreate {
  type: V1Monitor['type'];
  query: string;
  provider: string | null;
  provider_id?: string | null;
  /** Optional adapter profile; ignored by older servers. */
  search_profile?: Record<string, unknown>;
  action: V1Monitor['action'];
  add_to_kobo: boolean;
  scan_interval_seconds?: number;
}

export interface V1MonitorUpdate {
  status?: V1Monitor['status'];
  action?: V1Monitor['action'];
  add_to_kobo?: boolean;
  /** Fields reserved for servers that support editing monitor definitions. */
  provider_id?: string | null;
  search_profile?: Record<string, unknown>;
  scan_interval_seconds?: number;
}

export interface V1MonitorDiscovery {
  id: number;
  monitor_id: number;
  provider: string;
  provider_book_id: string;
  book_id: number | null;
  state: 'discovered' | 'present' | 'requested' | 'ignored' | 'future';
  payload_json: string;
  discovered_at: string;
}

export interface V1PushSubscription {
  id: number;
  device_label: string | null;
  created_at: string;
  last_success_at: string | null;
}

export interface V1PushRule {
  event_type: string;
  enabled: boolean;
  filters: Record<string, unknown>;
}

export interface V1PatcherManifest {
  available: boolean;
  filename?: string;
  sha256?: string;
  size_bytes?: number;
  protocol_version: number;
  download_url?: string;
  manual_endpoint_suffix?: string;
}

export interface V1Backup {
  id: string;
  created_at: string;
  size_bytes: number;
  verified_at: string | null;
  encrypted: boolean;
}

export interface V1MetadataReport {
  id: number;
  book_id: number;
  fields: string[];
  explanation: string;
  status: 'open' | 'accepted' | 'rejected';
  created_at: string;
  current_values: Record<string, unknown>;
  provider_values: V1MetadataProviderValue[];
}

export interface V1MetadataProviderValue {
  source: string;
  source_identifier: string | null;
  values: Record<string, unknown>;
}

export interface V1MetadataRevision {
  id: number;
  book_id: number;
  actor_user_id: number | null;
  reason: string;
  source: string;
  before_json: string;
  after_json: string;
  created_at: string;
}

export interface V1MetadataPreview {
  book_id: number;
  current_values: Record<string, unknown>;
  provider_values: V1MetadataProviderValue[];
  changes: Array<{ field: string; before: unknown; after: unknown }>;
}

export interface V1BookFile {
  id: number;
  file_format: string;
  size_bytes: number;
  mime_type: string | null;
  imported_at: string;
  granted_at: string;
  browser_reader?: 'reflowable' | 'fixed' | null;
  content_url?: string | null;
  download_url?: string;
}

export interface V1Book {
  id: number;
  title: string;
  author: string | null;
  language: string | null;
  publish_year: number | null;
  cover_url: string | null;
  isbn?: string | null;
  isbn_13?: string | null;
  formats: string[];
  request_status: V1Request['status'] | null;
  request_process_status: string | null;
  kobo_state: 'active' | 'deleted' | null;
}

export interface V1BookDetail {
  book: V1Book;
  files: V1BookFile[];
  entitled: boolean;
  request:
    | (V1Request & {
        request_process_status: string | null;
        monitoring_enabled: 0 | 1 | null;
        next_search_at: string | null;
      })
    | null;
  kobo: { desired_state: 'active' | 'deleted'; revision: number } | null;
}

export interface V1Request {
  id: number;
  book_id: number;
  request_process_id: number | null;
  status: 'pending_approval' | 'approved' | 'fulfilled' | 'rejected' | 'cancelled';
  content_type: 'ebook';
  add_to_kobo_when_available: 0 | 1;
  created_at: string;
  updated_at: string;
  /** Selected named profile; the server retains a snapshot on the request process. */
  profile_id?: number | null;
  profile_name?: string | null;
  profile_snapshot_hash?: string | null;
}

export interface V1AdminRequest extends V1Request {
  book_title: string;
  book_author?: string | null;
  requester_name?: string | null;
}

export interface V1RequestProcess {
  id: number;
  book_id: number;
  status: string;
  monitoring_enabled: 0 | 1;
  next_search_at: string | null;
  active_attempt_id: number | null;
  profile_id?: number | null;
  profile_name?: string | null;
  profile_snapshot_hash?: string | null;
}

export interface V1Candidate {
  id: number;
  request_process_id: number;
  accepted: 0 | 1;
  total_score: number;
  file_format: string | null;
  title?: string | null;
  source?: string | null;
  indexer?: string | null;
  protocol?: string | null;
  language?: string | null;
  size_bytes?: number | null;
  seeders?: number | null;
  peers?: number | null;
  score_components?: Record<string, number>;
  reject_reasons?: string[];
  blocked?: 0 | 1;
  selected_by_admin?: 0 | 1;
  selected_at?: string | null;
}

export interface V1RequestSearchRun {
  id: number;
  request_process_id: number;
  status: 'queued' | 'running' | 'completed' | 'partial' | 'failed';
  source_count: number;
  candidate_count: number;
  errors_json: string;
  started_at: string;
  completed_at: string | null;
}

export interface V1Attempt {
  id: number;
  request_process_id: number;
  status:
    | 'queued'
    | 'submitted'
    | 'downloading'
    | 'importing'
    | 'completed'
    | 'retry_scheduled'
    | 'failed'
    | 'cancelled';
  attempt_number: number;
  retry_count: number;
  candidate_id?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at?: string;
}

export interface V1ReleaseProfile {
  id: number;
  name: string;
  is_default: boolean;
  is_active: boolean;
  config: V1ReleaseProfileConfig;
  created_at: string;
  updated_at: string;
}

export interface V1ReleaseProfileConfig {
  allowed_formats: string[];
  allowed_languages: string[];
  allowed_protocols: string[];
  allowed_indexers: string[];
  min_seeders: number;
  max_size_bytes: number | null;
  allow_bundles: boolean;
  allow_archives: boolean;
  minimum_automatic_score: number;
  score_weights: Record<string, number>;
  term_scores: Record<string, number>;
  indexer_priorities: Record<string, number>;
}

export interface V1ReleaseProfilePreview {
  evaluation: {
    automatic_eligible: boolean;
    total_score: number;
    components: Record<string, number>;
    reject_reasons: string[];
    fingerprint: string;
  };
}

export interface V1MaintenanceStatus {
  database_integrity: string;
  pending_jobs: number;
  migrations: Array<{
    version: number;
    name: string;
    status: string;
    started_at: string | null;
    completed_at: string | null;
    error_message: string | null;
  }>;
}

export interface V1BestsellerSnapshotStatus {
  year: number;
  week: number;
  fetched_at: string;
  completed_at: string;
}

export interface V1BestsellerAdminStatus {
  last_checked_at: string | null;
  last_success: V1BestsellerSnapshotStatus | null;
  last_error: string | null;
  next_check_at: string | null;
  lease_expires_at: string | null;
  active_snapshot: V1BestsellerSnapshotStatus | null;
}

export interface V1KoboSettings {
  enabled: boolean;
  current_revision: number;
  tokens: Array<{
    id: number;
    token_prefix: string;
    device_label: string | null;
    created_at: string;
    last_used_at: string | null;
  }>;
}
