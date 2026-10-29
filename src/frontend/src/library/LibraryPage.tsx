import { useCallback, useState } from 'react';
import { Link, Navigate, useSearchParams } from 'react-router-dom';

import { useLanguage } from '../contexts/LanguageContext';
import { useDependencyEffect } from '../hooks/useMountEffect';
import { v1Api } from '../services/v1Api';
import type { V1Book } from '../types/v1';
import { withBasePath } from '../utils/basePath';

type FileFilter = 'all' | 'with-files' | 'needs-files';
type LibrarySort = 'recent' | 'title' | 'author' | 'year';
type LibraryView = 'grid' | 'list';

const isFileFilter = (value: string | null): value is FileFilter =>
  value === 'with-files' || value === 'needs-files' || value === 'all';
const isLibrarySort = (value: string | null): value is LibrarySort =>
  value === 'title' || value === 'author' || value === 'year' || value === 'recent';

const Cover = ({ book }: { book: V1Book }) => {
  const [imageFailed, setImageFailed] = useState(false);
  const initial = (book.title?.trim()[0] ?? '?').toUpperCase();

  if (!book.cover_url || imageFailed) {
    return (
      <div className="bookflow-cover flex aspect-[2/3] w-full items-center justify-center rounded-xl bg-linear-to-br from-violet-700 via-indigo-700 to-slate-900 text-4xl font-semibold text-slate-100">
        {initial}
      </div>
    );
  }

  return (
    <img
      src={withBasePath(book.cover_url)}
      alt={book.title ?? ''}
      className="bookflow-cover aspect-[2/3] w-full rounded-xl object-cover"
      onError={() => setImageFailed(true)}
    />
  );
};

const FormatBadges = ({ formats }: { formats: V1Book['formats'] }) => {
  const uniqueFormats = [...new Set(formats)];
  if (!uniqueFormats.length) return null;

  return (
    <div className="flex flex-wrap gap-1">
      {uniqueFormats.map((format) => (
        <span
          key={format}
          className="rounded bg-(--hover-surface) px-1.5 py-0.5 text-[10px] font-bold tracking-wide"
        >
          {format.toUpperCase()}
        </span>
      ))}
    </div>
  );
};

const BookStatus = ({ book, koboEnabled }: { book: V1Book; koboEnabled: boolean }) => {
  const { t } = useLanguage();
  return (
    <div className="mt-2 flex flex-wrap gap-1 text-[10px] opacity-70">
      {book.request_status && (
        <span>
          {t('common.request')}: {book.request_status}
        </span>
      )}
      {book.request_process_status && (
        <span>
          {t('common.requestProcess')}: {book.request_process_status}
        </span>
      )}
      {koboEnabled && book.kobo_state === 'active' && <span>Kobo</span>}
    </div>
  );
};

export const LibraryPage = ({
  koboEnabled = false,
  koboOnly = false,
}: {
  koboEnabled?: boolean;
  koboOnly?: boolean;
}) => {
  const [params] = useSearchParams();
  if (!koboOnly && params.get('shelf') === 'kobo') {
    return <Navigate to="/kobo" replace />;
  }
  return <LibraryPageContent koboEnabled={koboEnabled} koboOnly={koboOnly} />;
};

const LibraryPageContent = ({
  koboEnabled = false,
  koboOnly = false,
}: {
  koboEnabled?: boolean;
  koboOnly?: boolean;
}) => {
  const { t } = useLanguage();
  const [params, setParams] = useSearchParams();
  const [books, setBooks] = useState<V1Book[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const q = params.get('q') ?? '';
  const [searchDraft, setSearchDraft] = useState(q);
  const fileStateParam = params.get('file_state');
  const filter: FileFilter = isFileFilter(fileStateParam) ? fileStateParam : 'all';
  const format = params.get('format') ?? '';
  const sortParam = params.get('sort');
  const sort: LibrarySort = isLibrarySort(sortParam) ? sortParam : 'recent';
  const view: LibraryView = params.get('view') === 'list' ? 'list' : 'grid';
  const koboShelf = koboEnabled && koboOnly;
  const pageSize = 30;
  const offset = Math.max(0, Number(params.get('offset') ?? '0') || 0);
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await v1Api.library({
        q,
        file_state: filter,
        format,
        sort,
        shelf: koboShelf ? 'kobo' : undefined,
        offset,
        limit: pageSize,
      });
      setBooks(response.items);
      setTotal(response.total ?? response.items.length);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [filter, format, koboShelf, offset, q, sort, t]);

  useDependencyEffect(() => {
    void load();
  }, [load]);
  useDependencyEffect(() => {
    setSearchDraft(q);
  }, [q]);

  const update = (updates: Record<string, string>, resetPage = true) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        Object.entries(updates).forEach(([key, value]) => {
          if (value) next.set(key, value);
          else next.delete(key);
        });
        if (resetPage && !Object.hasOwn(updates, 'offset')) next.delete('offset');
        return next;
      },
      { replace: true },
    );
  const firstResult = total ? offset + 1 : 0;
  const lastResult = Math.min(offset + books.length, total);
  const hasFilters = Boolean(q || format || filter !== 'all');

  return (
    <section className="pb-16">
      <div className="mb-8 flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <p className="text-xs font-semibold tracking-widest text-violet-600 uppercase dark:text-violet-300">
            {t('library.eyebrow')}
          </p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight text-(--text)">
            {koboShelf ? t('library.koboTitle') : t('library.title')}
          </h1>
          <p className="mt-2 text-sm opacity-65">
            {t('library.summary', {
              count: total,
              works: total === 1 ? t('library.work') : t('library.works'),
            })}
          </p>
        </div>
      </div>

      <form
        className="bookflow-panel mb-6 grid gap-3 rounded-2xl p-3 sm:grid-cols-[1fr_auto_auto_auto]"
        onSubmit={(event) => {
          event.preventDefault();
          update({ q: searchDraft });
        }}
      >
        <input
          aria-label={t('library.search')}
          value={searchDraft}
          onChange={(event) => setSearchDraft(event.target.value)}
          placeholder={t('library.search')}
          className="min-w-0 rounded-md border border-(--border-muted) bg-transparent px-3 py-2 text-sm"
        />
        <select
          value={filter}
          onChange={(event) => update({ file_state: event.target.value })}
          aria-label={t('library.fileState')}
          className="rounded-md border border-(--border-muted) bg-transparent px-3 py-2 text-sm"
        >
          <option value="all">{t('library.all')}</option>
          <option value="with-files">{t('library.hasFiles')}</option>
          <option value="needs-files">{t('library.needsFiles')}</option>
        </select>
        <select
          value={format}
          onChange={(event) => update({ format: event.target.value })}
          aria-label={t('library.format')}
          className="rounded-md border border-(--border-muted) bg-transparent px-3 py-2 text-sm"
        >
          <option value="">{t('library.allFormats')}</option>
          <option value="epub">EPUB</option>
          <option value="kepub">KEPUB</option>
          <option value="pdf">PDF</option>
          <option value="mobi">MOBI</option>
        </select>
        <select
          value={sort}
          onChange={(event) => update({ sort: event.target.value })}
          aria-label={t('library.sort')}
          className="rounded-md border border-(--border-muted) bg-transparent px-3 py-2 text-sm"
        >
          <option value="recent">{t('library.sortRecent')}</option>
          <option value="title">{t('library.sortTitle')}</option>
          <option value="author">{t('library.sortAuthor')}</option>
          <option value="year">{t('library.sortYear')}</option>
        </select>
      </form>

      <div className="mb-5 flex flex-wrap items-center justify-between gap-3 text-sm">
        {!loading && !error && (
          <p className="opacity-65">
            {t('library.pageResults', { from: firstResult, to: lastResult, total })}
          </p>
        )}
        <div className="flex rounded-md border border-(--border-muted) p-0.5 text-xs">
          <button
            type="button"
            aria-pressed={view === 'grid'}
            className={`rounded px-2.5 py-1.5 ${view === 'grid' ? 'bg-(--hover-surface) font-semibold' : 'opacity-65'}`}
            onClick={() => update({ view: 'grid' }, false)}
          >
            {t('library.grid')}
          </button>
          <button
            type="button"
            aria-pressed={view === 'list'}
            className={`rounded px-2.5 py-1.5 ${view === 'list' ? 'bg-(--hover-surface) font-semibold' : 'opacity-65'}`}
            onClick={() => update({ view: 'list' }, false)}
          >
            {t('library.list')}
          </button>
        </div>
      </div>

      {loading && <LibrarySkeleton />}
      {error && (
        <div className="bookflow-panel rounded-2xl p-6 text-center">
          <p className="text-sm text-(--text)">{error}</p>
          <button
            type="button"
            className="mt-3 text-sm text-emerald-700 underline"
            onClick={() => void load()}
          >
            {t('common.retry')}
          </button>
        </div>
      )}
      {!loading && !error && !books.length && (
        <div className="bookflow-panel rounded-2xl p-8 text-center">
          <h2 className="font-semibold text-(--text)">
            {hasFilters ? t('library.noMatch') : t('library.empty')}
          </h2>
          <p className="mt-2 text-sm opacity-65">
            {hasFilters ? t('library.noMatchHelp') : t('library.emptyHelp')}
          </p>
        </div>
      )}
      {!loading && !error && books.length > 0 && (
        <div
          className={
            view === 'grid'
              ? 'grid grid-cols-2 gap-x-4 gap-y-8 sm:grid-cols-3 lg:grid-cols-5'
              : 'bookflow-panel divide-y rounded-2xl'
          }
        >
          {books.map((book) => (
            <article key={book.id} className={view === 'grid' ? 'group min-w-0' : 'flex gap-4 p-4'}>
              <Link
                to={koboOnly ? `/kobo/${book.id}` : `/library/${book.id}`}
                className={view === 'grid' ? 'block' : 'contents'}
              >
                <div className={view === 'grid' ? '' : 'w-16 shrink-0'}>
                  <Cover book={book} />
                </div>
                <div className={view === 'grid' ? '' : 'min-w-0 flex-1'}>
                  <h2
                    className={
                      view === 'grid'
                        ? 'mt-3 truncate font-semibold text-(--text)'
                        : 'truncate font-semibold text-(--text)'
                    }
                  >
                    {book.title ?? t('library.untitled')}
                  </h2>
                  <p className="truncate text-sm opacity-65">
                    {book.author || t('common.unknownAuthor')}
                  </p>
                  {view === 'list' && book.publish_year && (
                    <p className="mt-1 text-xs opacity-60">{book.publish_year}</p>
                  )}
                </div>
              </Link>
              <div className={view === 'grid' ? 'mt-2' : 'self-end'}>
                <FormatBadges formats={book.formats} />
                <BookStatus book={book} koboEnabled={koboEnabled} />
              </div>
            </article>
          ))}
        </div>
      )}
      {!loading && !error && total > 0 && (
        <nav
          className="mt-8 flex items-center justify-between gap-3"
          aria-label={t('library.pagination')}
        >
          <button
            type="button"
            className="rounded border border-(--border-muted) px-3 py-2 text-sm disabled:opacity-50"
            disabled={!offset}
            onClick={() => update({ offset: String(Math.max(0, offset - pageSize)) }, false)}
          >
            {t('library.previous')}
          </button>
          <button
            type="button"
            className="rounded border border-(--border-muted) px-3 py-2 text-sm disabled:opacity-50"
            disabled={offset + books.length >= total}
            onClick={() => update({ offset: String(offset + pageSize) }, false)}
          >
            {t('library.next')}
          </button>
        </nav>
      )}
    </section>
  );
};

const LibrarySkeleton = () => (
  <div className="grid animate-pulse grid-cols-2 gap-x-4 gap-y-8 sm:grid-cols-3 lg:grid-cols-5">
    {Array.from({ length: 10 }, (_, index) => (
      <div key={index}>
        <div className="aspect-[2/3] rounded-lg bg-gray-200 dark:bg-gray-700" />
        <div className="mt-3 h-4 rounded bg-gray-200 dark:bg-gray-700" />
        <div className="mt-2 h-3 w-2/3 rounded bg-gray-200 dark:bg-gray-700" />
      </div>
    ))}
  </div>
);
