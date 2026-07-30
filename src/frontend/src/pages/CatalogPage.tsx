import { useCallback, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

import { BookSelectionControl } from '../components/BookSelectionControl';
import { BulkLibraryActionBar } from '../components/BulkLibraryActionBar';
import { DiscoverNavigation } from '../components/DiscoverNavigation';
import { useLanguage } from '../contexts/LanguageContext';
import { useBulkLibrarySelection } from '../hooks/useBulkLibrarySelection';
import { useDependencyEffect } from '../hooks/useMountEffect';
import { resolveCatalogBook, searchMetadata } from '../services/api';
import { v1Api } from '../services/v1Api';
import type { Book } from '../types';
import type { V1CatalogBook } from '../types/v1';
import { withBasePath } from '../utils/basePath';
import { catalogBookPath } from '../utils/catalogBookPath';

const Cover = ({ book, alt }: { book: V1CatalogBook; alt: string }) =>
  book.cover_url ? (
    <img
      className="bookflow-cover aspect-[2/3] w-full rounded-xl object-cover"
      src={withBasePath(book.cover_url)}
      alt={alt}
    />
  ) : (
    <div className="bookflow-cover flex aspect-[2/3] items-center justify-center rounded-xl bg-linear-to-br from-violet-700 via-indigo-700 to-slate-900 text-3xl font-semibold text-white">
      {book.title.slice(0, 1).toUpperCase()}
    </div>
  );

export const CatalogPage = () => {
  const { t } = useLanguage();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [books, setBooks] = useState<V1CatalogBook[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [externalBooks, setExternalBooks] = useState<Book[]>([]);
  const [externalLoading, setExternalLoading] = useState(false);
  const [externalError, setExternalError] = useState<string | null>(null);
  const [openingExternalId, setOpeningExternalId] = useState<string | null>(null);
  const q = params.get('q') ?? '';
  const external = params.get('external') === '1';
  const externalSearchKey = useRef<string | null>(null);
  const [searchDraft, setSearchDraft] = useState(q);
  const format = params.get('format') ?? '';
  const sort = params.get('sort') ?? 'recent';
  const view = params.get('view') === 'list' ? 'list' : 'grid';
  const authorId = params.get('author_id') ?? '';
  const seriesId = params.get('series_id') ?? '';
  const pageSize = 30;
  const offset = Math.max(0, Number(params.get('offset') ?? '0') || 0);
  const bulk = useBulkLibrarySelection(JSON.stringify({ q, format, sort, authorId, seriesId }));
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await v1Api.catalog({
        q,
        format,
        sort,
        authorId,
        seriesId,
        offset,
        limit: pageSize,
      });
      setBooks(page.items);
      setTotal(page.total ?? page.items.length);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [authorId, format, offset, q, seriesId, sort, t]);
  useDependencyEffect(() => {
    void load();
  }, [load]);
  useDependencyEffect(() => {
    setSearchDraft(q);
    setExternalBooks([]);
    setExternalError(null);
  }, [q]);
  const update = (updates: Record<string, string>, resetPage = true) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        Object.entries(updates).forEach(([key, value]) =>
          value ? next.set(key, value) : next.delete(key),
        );
        if (resetPage && !Object.hasOwn(updates, 'offset')) next.delete('offset');
        return next;
      },
      { replace: true },
    );
  const loadExternal = useCallback(async () => {
    if (!q.trim()) return;
    setExternalLoading(true);
    setExternalError(null);
    try {
      const results = (await searchMetadata(q.trim(), 40, 'relevance', {}, 1, 'ebook')).books;
      const presence = await v1Api.catalogPresence(
        results.map((book) => ({
          provider: book.provider,
          provider_book_id: book.provider_id,
          isbn: book.isbn_13 ?? book.isbn_10,
        })),
      );
      const presentIndexes = new Set(presence.present);
      setExternalBooks(results.filter((_book, index) => !presentIndexes.has(index)));
    } catch (caught) {
      setExternalError(caught instanceof Error ? caught.message : t('bookAdd.searchFailed'));
    } finally {
      setExternalLoading(false);
    }
  }, [q, t]);
  useDependencyEffect(() => {
    if (external && q.trim() && externalSearchKey.current !== q) {
      externalSearchKey.current = q;
      void loadExternal();
    }
  }, [external, loadExternal, q]);
  const openExternal = async (book: Book) => {
    if (!book.provider || !book.provider_id) return;
    setOpeningExternalId(book.id);
    setExternalError(null);
    try {
      const result = await resolveCatalogBook(book.provider, book.provider_id);
      await navigate(catalogBookPath({ id: result.book_id, isbn_13: result.isbn_13 }));
    } catch (caught) {
      setExternalError(caught instanceof Error ? caught.message : t('bookAdd.openFailed'));
    } finally {
      setOpeningExternalId(null);
    }
  };
  return (
    <section className="pb-16">
      <div className="mb-7">
        <p className="text-xs font-semibold tracking-widest text-violet-600 uppercase dark:text-violet-300">
          {t('catalog.eyebrow')}
        </p>
        <h1 className="mt-1 text-3xl font-semibold tracking-tight">{t('catalog.title')}</h1>
        <p className="mt-2 text-sm opacity-65">{t('catalog.subtitle')}</p>
      </div>
      <DiscoverNavigation />
      <div className="mb-4 flex justify-end">
        <button
          type="button"
          onClick={() => bulk.setActive(!bulk.active)}
          className={`rounded-md border px-3 py-2 text-sm font-medium ${
            bulk.active ? 'border-violet-500 bg-violet-600 text-white' : 'border-(--border-muted)'
          }`}
        >
          {bulk.active ? t('bulkLibrary.cancelSelection') : t('bulkLibrary.select')}
        </button>
      </div>
      <form
        className="bookflow-panel mb-6 grid gap-3 rounded-2xl p-3 sm:grid-cols-[1fr_auto_auto_auto]"
        onSubmit={(event) => {
          event.preventDefault();
          update({ q: searchDraft });
        }}
      >
        <input
          value={searchDraft}
          onChange={(event) => setSearchDraft(event.target.value)}
          placeholder={t('catalog.search')}
          className="rounded-md border border-(--border-muted) bg-transparent px-3 py-2 text-sm"
          aria-label={t('catalog.search')}
        />
        <select
          value={format}
          onChange={(event) => update({ format: event.target.value })}
          className="rounded-md border border-(--border-muted) bg-transparent px-3 py-2 text-sm"
          aria-label={t('catalog.formatLabel')}
        >
          <option value="">{t('catalog.allFormats')}</option>
          <option value="epub">EPUB</option>
          <option value="kepub">KEPUB</option>
          <option value="pdf">PDF</option>
          <option value="mobi">MOBI</option>
        </select>
        <select
          value={sort}
          onChange={(event) => update({ sort: event.target.value })}
          className="rounded-md border border-(--border-muted) bg-transparent px-3 py-2 text-sm"
          aria-label={t('catalog.sortLabel')}
        >
          <option value="recent">{t('catalog.recent')}</option>
          <option value="title">{t('catalog.titleSort')}</option>
          <option value="author">{t('catalog.authorSort')}</option>
          <option value="year">{t('catalog.yearSort')}</option>
        </select>
        <button
          type="button"
          onClick={() => update({ view: view === 'grid' ? 'list' : 'grid' }, false)}
          className="rounded-md border border-(--border-muted) px-3 py-2 text-sm font-medium"
        >
          {view === 'grid' ? t('catalog.list') : t('catalog.grid')}
        </button>
      </form>
      <div className="mb-5 flex gap-2 text-sm">
        {(authorId || seriesId) && (
          <button
            type="button"
            className="underline"
            onClick={() => update({ author_id: '', series_id: '', offset: '' })}
          >
            {t('catalog.clearFilter')}
          </button>
        )}
      </div>
      {!loading && !error && (
        <p className="mb-4 text-sm opacity-65">{t('common.results', { count: total })}</p>
      )}
      {loading && <p className="py-10 text-sm opacity-65">{t('catalog.loading')}</p>}
      {error && (
        <div className="bookflow-panel rounded-2xl border-red-500/30 p-5 text-sm text-red-600">
          {error}{' '}
          <button type="button" className="underline" onClick={() => void load()}>
            {t('common.retry')}
          </button>
        </div>
      )}
      {!loading && !error && !books.length && (
        <p className="bookflow-panel rounded-2xl p-8 text-center text-sm opacity-65">
          {t('catalog.empty')}
        </p>
      )}
      {!loading && !error && books.length > 0 && (
        <>
          <div
            className={
              view === 'grid'
                ? 'grid grid-cols-2 gap-x-4 gap-y-8 sm:grid-cols-3 lg:grid-cols-5'
                : 'bookflow-panel divide-y rounded-2xl'
            }
          >
            {books.map((book) => (
              <article
                key={book.id}
                className={`relative ${view === 'grid' ? 'min-w-0' : 'flex gap-4 p-4'}`}
              >
                <Link
                  to={catalogBookPath(book)}
                  tabIndex={bulk.active ? -1 : undefined}
                  className={view === 'grid' ? 'block' : 'contents'}
                >
                  <div className={view === 'grid' ? '' : 'w-16 shrink-0'}>
                    <Cover book={book} alt={t('common.coverOf', { title: book.title })} />
                  </div>
                  <div className="min-w-0">
                    <h2 className="mt-2 truncate font-semibold">{book.title}</h2>
                    <p className="truncate text-sm opacity-65">
                      {book.author || t('common.unknownAuthor')}
                    </p>
                    {book.series_name && (
                      <p className="mt-1 truncate text-xs opacity-55">{book.series_name}</p>
                    )}
                    {(book.formats?.length ?? 0) > 0 && (
                      <p className="mt-2 text-[11px] font-medium uppercase opacity-60">
                        {book.formats?.join(' · ')}
                      </p>
                    )}
                  </div>
                </Link>
                <BookSelectionControl
                  active={bulk.active}
                  inLibrary={Boolean(book.in_library)}
                  selected={bulk.selected.has(book.id)}
                  title={book.title}
                  onToggle={() => bulk.toggle(book.id)}
                />
              </article>
            ))}
          </div>
          <nav
            className="mt-8 flex items-center justify-between gap-3"
            aria-label={t('catalog.title')}
          >
            <button
              type="button"
              disabled={offset === 0}
              className="rounded-md border border-(--border-muted) px-3 py-2 text-sm disabled:opacity-50"
              onClick={() => update({ offset: String(Math.max(0, offset - pageSize)) }, false)}
            >
              {t('catalog.previous')}
            </button>
            <span className="text-sm opacity-65">
              {offset + 1}–{Math.min(offset + books.length, total)} / {total}
            </span>
            <button
              type="button"
              disabled={offset + books.length >= total}
              className="rounded-md border border-(--border-muted) px-3 py-2 text-sm disabled:opacity-50"
              onClick={() => update({ offset: String(offset + pageSize) }, false)}
            >
              {t('catalog.next')}
            </button>
          </nav>
        </>
      )}
      {q.trim() && (
        <section className="bookflow-panel mt-10 rounded-2xl p-5" aria-live="polite">
          <h2 className="font-semibold">{t('bookAdd.external')}</h2>
          <p className="mt-1 text-sm opacity-65">{t('catalog.externalHelp')}</p>
          {!externalBooks.length && !externalLoading && (
            <button
              type="button"
              className="mt-4 rounded-md border border-(--border-muted) px-3 py-2 text-sm font-medium"
              onClick={() => void loadExternal()}
            >
              {t('bookAdd.external')}
            </button>
          )}
          {externalLoading && <p className="mt-4 text-sm opacity-65">{t('bookAdd.searching')}</p>}
          {externalError && (
            <p className="mt-4 text-sm text-red-600 dark:text-red-400">{externalError}</p>
          )}
          {externalBooks.length > 0 && (
            <div className="mt-4 divide-y divide-(--border-muted)">
              {externalBooks.map((book) => (
                <button
                  key={`${book.provider}:${book.provider_id}:${book.id}`}
                  type="button"
                  disabled={openingExternalId === book.id || !book.provider || !book.provider_id}
                  className="flex w-full items-center gap-3 rounded-lg px-2 py-3 text-left hover:bg-(--hover-surface) disabled:opacity-60"
                  onClick={() => void openExternal(book)}
                >
                  {book.preview && (
                    <img
                      src={withBasePath(book.preview)}
                      alt=""
                      className="h-14 w-10 rounded object-cover"
                    />
                  )}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{book.title}</span>
                    <span className="block truncate text-sm opacity-65">
                      {book.author || t('common.unknownAuthor')}
                      {book.year ? ` · ${book.year}` : ''}
                    </span>
                  </span>
                  <span className="text-sm font-medium text-violet-600 dark:text-violet-300">
                    {openingExternalId === book.id ? t('bookAdd.opening') : t('bookAdd.view')}
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>
      )}
      <BulkLibraryActionBar
        visible={bulk.active}
        selectedCount={bulk.selected.size}
        busy={bulk.busy}
        limitReached={bulk.limitReached}
        response={bulk.response}
        error={bulk.error}
        onSelectPage={() => bulk.selectPage(books)}
        onClear={bulk.clear}
        onCancel={() => bulk.setActive(false)}
        onSubmit={() =>
          void bulk.submit((successfulIds) => {
            setBooks((current) =>
              current.map((book) =>
                successfulIds.has(book.id) ? { ...book, in_library: true } : book,
              ),
            );
          })
        }
      />
    </section>
  );
};
