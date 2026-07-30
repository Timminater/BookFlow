import { useCallback, useState } from 'react';

import { BulkLibraryActionBar } from '../components/BulkLibraryActionBar';
import { DashboardShelf, PosterCard, RequestCard } from '../components/dashboard';
import { useLanguage } from '../contexts/LanguageContext';
import { useBulkLibrarySelection } from '../hooks/useBulkLibrarySelection';
import { useDependencyEffect } from '../hooks/useMountEffect';
import { useOfflineReading } from '../library/offline/OfflineReadingCoordinator';
import { v1Api } from '../services/v1Api';
import type {
  V1CatalogBook,
  V1ReadingProgress,
  V1SuggestionsResponse,
  V1UserRequest,
} from '../types/v1';
import { catalogBookPath } from '../utils/catalogBookPath';
import {
  dashboardSuggestionLists,
  loadDashboardData,
  showOptionalDashboardShelf,
  type DashboardSectionKey,
} from './dashboardData';

export const DashboardPage = () => {
  const { t } = useLanguage();
  const [recent, setRecent] = useState<V1CatalogBook[]>([]);
  const [requests, setRequests] = useState<V1UserRequest[]>([]);
  const [reading, setReading] = useState<V1ReadingProgress[]>([]);
  const [suggestions, setSuggestions] = useState<V1SuggestionsResponse>({
    snapshot: null,
    lists: [],
  });
  const [errors, setErrors] = useState<Partial<Record<DashboardSectionKey, string>>>({});
  const [loading, setLoading] = useState(true);
  const trendingBulk = useBulkLibrarySelection('dashboard-trending');
  const {
    cachedFileIds,
    status: offlineCacheStatus,
    failedBookCount,
    failures,
    retryOfflineCache,
  } = useOfflineReading();

  const load = useCallback(async () => {
    setLoading(true);
    const data = await loadDashboardData(v1Api);
    setRecent(data.recent);
    setRequests(data.requests);
    setReading(data.reading);
    setSuggestions(data.suggestions);
    setErrors(data.errors);
    setLoading(false);
  }, []);

  useDependencyEffect(() => {
    void load();
  }, [load]);

  const trending = dashboardSuggestionLists(suggestions)[0];
  const sectionError = (key: DashboardSectionKey, fallbackKey: Parameters<typeof t>[0]) =>
    errors[key] ? t(fallbackKey) : undefined;

  return (
    <section className="space-y-8 pt-1 pb-16 sm:space-y-10">
      {offlineCacheStatus === 'error' && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
          {failedBookCount > 0
            ? t('offline.onlineCacheFailure', { count: failedBookCount })
            : t('offline.cacheFailure')}
          {failures.length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className="opacity-80">
                {failures
                  .map((failure) => `${failure.title ?? failure.bookId} (${failure.errorCode})`)
                  .join(', ')}
              </span>
              <button
                type="button"
                className="rounded border border-amber-500/50 px-2 py-1 text-xs font-medium"
                onClick={() => void retryOfflineCache()}
              >
                {t('offline.retry')}
              </button>
            </div>
          )}
        </div>
      )}
      <DashboardShelf
        id="recent-heading"
        title={t('dashboard.recent')}
        href="/catalog"
        hrefLabel={t('dashboard.viewCatalog')}
        itemCount={recent.length}
        loading={loading}
        error={sectionError('recent', 'dashboard.recentLoadFailed')}
        emptyMessage={t('dashboard.recentEmpty')}
        onRetry={() => void load()}
      >
        {recent.slice(0, 12).map((book) => (
          <PosterCard
            key={book.id}
            to={catalogBookPath(book)}
            title={book.title}
            author={book.author}
            coverUrl={book.cover_url}
            badge={t('dashboard.newBadge')}
            badgeTone="blue"
          />
        ))}
      </DashboardShelf>

      {showOptionalDashboardShelf(loading, errors.requests, requests.length) && (
        <DashboardShelf
          id="requests-heading"
          title={t('dashboard.recentRequests')}
          itemCount={requests.length}
          loading={loading}
          error={sectionError('requests', 'dashboard.requestsLoadFailed')}
          emptyMessage={t('dashboard.requestsEmpty')}
          onRetry={() => void load()}
          skeletonVariant="wide"
        >
          {requests.map((request) => (
            <RequestCard key={request.id} request={request} />
          ))}
        </DashboardShelf>
      )}

      {showOptionalDashboardShelf(loading, errors.reading, reading.length) && (
        <DashboardShelf
          id="reading-heading"
          title={t('dashboard.continue')}
          href="/library"
          hrefLabel={t('dashboard.viewLibrary')}
          itemCount={reading.length}
          loading={loading}
          error={sectionError('reading', 'dashboard.readingLoadFailed')}
          emptyMessage={t('dashboard.continueEmpty')}
          onRetry={() => void load()}
        >
          {reading.slice(0, 10).map((item) => {
            const offlineAvailable =
              typeof item.file_id === 'number' && cachedFileIds.has(item.file_id);
            return (
              <PosterCard
                key={item.book_id}
                to={`/library/${item.book_id}`}
                title={item.title ?? t('dashboard.bookFallback')}
                author={item.author}
                coverUrl={item.cover_url}
                badge={
                  offlineAvailable
                    ? t('offline.availableBadge', { progress: Math.round(item.progress) })
                    : `${Math.round(item.progress)}%`
                }
                badgeTone="green"
                progress={item.progress}
              />
            );
          })}
        </DashboardShelf>
      )}

      <DashboardShelf
        id="trending-heading"
        title={t('dashboard.trendingShelf')}
        href="/suggestions/overall"
        hrefLabel={t('dashboard.viewTrending')}
        meta={
          <div className="flex items-center gap-2">
            {suggestions.snapshot && (
              <span className="hidden rounded-full border border-(--border-muted) bg-(--bg-soft) px-2.5 py-1 text-[11px] opacity-60 sm:inline">
                {t('dashboard.trendingWeek', {
                  week: suggestions.snapshot.week,
                  year: suggestions.snapshot.year,
                })}
              </span>
            )}
            <button
              type="button"
              onClick={() => trendingBulk.setActive(!trendingBulk.active)}
              className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${
                trendingBulk.active
                  ? 'border-violet-500 bg-violet-600 text-white'
                  : 'border-(--border-muted)'
              }`}
            >
              {trendingBulk.active ? t('bulkLibrary.cancelSelection') : t('bulkLibrary.select')}
            </button>
          </div>
        }
        itemCount={trending?.items.length ?? 0}
        loading={loading}
        error={sectionError('suggestions', 'dashboard.trendingLoadFailed')}
        emptyMessage={t('dashboard.trendingEmpty')}
        onRetry={() => void load()}
      >
        {(trending?.items ?? []).map((book, index) => (
          <PosterCard
            key={book.id}
            to={`/suggestions/overall/${book.id}`}
            title={book.title}
            author={book.author}
            coverUrl={book.cover_url}
            badge={`#${index + 1}`}
            selectionActive={trendingBulk.active}
            inLibrary={book.in_library}
            selected={trendingBulk.selected.has(book.id)}
            onToggleSelection={() => trendingBulk.toggle(book.id)}
          />
        ))}
      </DashboardShelf>
      <BulkLibraryActionBar
        visible={trendingBulk.active}
        selectedCount={trendingBulk.selected.size}
        busy={trendingBulk.busy}
        limitReached={trendingBulk.limitReached}
        response={trendingBulk.response}
        error={trendingBulk.error}
        onSelectPage={() => trendingBulk.selectPage(trending?.items ?? [])}
        onClear={trendingBulk.clear}
        onCancel={() => trendingBulk.setActive(false)}
        onSubmit={() =>
          void trendingBulk.submit((successfulIds) => {
            setSuggestions((current) => ({
              ...current,
              lists: current.lists.map((list) => ({
                ...list,
                items: list.items.map((book) =>
                  successfulIds.has(book.id) ? { ...book, in_library: true } : book,
                ),
              })),
            }));
          })
        }
      />
    </section>
  );
};
