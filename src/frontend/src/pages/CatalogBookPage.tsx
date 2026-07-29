import { useCallback, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { useLanguage } from '../contexts/LanguageContext';
import { useDependencyEffect } from '../hooks/useMountEffect';
import type { TranslationKey } from '../i18n/translations';
import { downloadBookFile, fetchBookFile, shareBookFile } from '../reader/fileActions';
import { canReadInBrowser } from '../reader/formatCapabilities';
import { addLibraryBook, removeLibraryBook } from '../services/api';
import { v1Api, type V1CatalogDeletionPlan } from '../services/v1Api';
import type { V1BookDetail, V1CatalogBookDetail, V1ReleaseProfile, V1Request } from '../types/v1';
import { withBasePath } from '../utils/basePath';
import { createIdempotencyKey } from '../utils/idempotencyKey';
import {
  blocksNewRequest,
  catalogPrimaryAction,
  presentBookDetail,
  requestStatusKind,
  resolveCatalogBookReference,
} from './bookDetailPresentation';

interface CatalogBookPageProps {
  canAddToLibrary: boolean;
  isAdmin: boolean;
}

const REQUEST_LABELS: Record<V1Request['status'], TranslationKey> = {
  pending_approval: 'catalogDetail.request.pending_approval',
  approved: 'catalogDetail.request.approved',
  fulfilled: 'catalogDetail.request.fulfilled',
  rejected: 'catalogDetail.request.rejected',
  cancelled: 'catalogDetail.request.cancelled',
};

const REQUEST_TONE_CLASSES = {
  progress: 'border-violet-400/30 bg-violet-500/15 text-violet-700 dark:text-violet-200',
  success: 'border-emerald-400/30 bg-emerald-500/15 text-emerald-700 dark:text-emerald-200',
  danger: 'border-red-400/30 bg-red-500/15 text-red-700 dark:text-red-200',
  neutral: 'border-slate-400/25 bg-slate-500/15 text-slate-700 dark:text-slate-200',
} as const;

const Fact = ({ label, value }: { label: string; value: string | number }) => (
  <div className="min-w-0">
    <dt className="text-[10px] font-semibold tracking-[0.16em] text-slate-400 uppercase">
      {label}
    </dt>
    <dd className="mt-1 truncate text-sm font-medium text-white" title={String(value)}>
      {value}
    </dd>
  </div>
);

const formatDate = (value: string | null | undefined, language: 'en' | 'nl') => {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(language === 'nl' ? 'nl-NL' : 'en-US', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  }).format(date);
};

const formatRequestError = (caught: unknown, fallback: string): string => {
  if (
    caught instanceof DOMException &&
    (caught.name === 'TimeoutError' || caught.name === 'AbortError')
  ) {
    return 'The server did not respond in time. Please try again.';
  }
  return caught instanceof Error ? caught.message : fallback;
};

export const CatalogBookPage = ({ canAddToLibrary, isAdmin }: CatalogBookPageProps) => {
  const { language, t } = useLanguage();
  const { bookId: rawBookId, isbn: rawIsbn } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<V1CatalogBookDetail | null>(null);
  const [libraryBook, setLibraryBook] = useState<V1BookDetail | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [addDialogError, setAddDialogError] = useState<string | null>(null);
  const [loadingAddOptions, setLoadingAddOptions] = useState(false);
  const [koboEnabledForAdd, setKoboEnabledForAdd] = useState(false);
  const [addLibraryToKobo, setAddLibraryToKobo] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [confirmRemoval, setConfirmRemoval] = useState(false);
  const [confirmCatalogDeletion, setConfirmCatalogDeletion] = useState(false);
  const [catalogDeletionBusy, setCatalogDeletionBusy] = useState(false);
  const [catalogDeletionToken, setCatalogDeletionToken] = useState<string | null>(null);
  const [catalogDeletionPlan, setCatalogDeletionPlan] = useState<V1CatalogDeletionPlan | null>(
    null,
  );
  const [catalogDeletionConfirmation, setCatalogDeletionConfirmation] = useState('');
  const [catalogCleanupOperationId, setCatalogCleanupOperationId] = useState<string | null>(null);
  const [requestDialogOpen, setRequestDialogOpen] = useState(false);
  const [profiles, setProfiles] = useState<V1ReleaseProfile[]>([]);
  const [profileId, setProfileId] = useState<number | null>(null);
  const [addToKobo, setAddToKobo] = useState(false);
  const [reportField, setReportField] = useState('title');
  const [reportNote, setReportNote] = useState('');
  const [fileAction, setFileAction] = useState<string | null>(null);
  const [preparedFileStates, setPreparedFileStates] = useState<
    Record<number, 'preparing' | 'ready' | 'failed'>
  >({});
  const preparedFiles = useRef(new Map<number, File>());
  const preparingFiles = useRef(new Set<number>());
  const preparedFileGeneration = useRef(0);

  const load = useCallback(async () => {
    setDetail(null);
    setLibraryBook(null);
    setMessage(null);
    const reference = resolveCatalogBookReference(rawBookId, rawIsbn);
    if (!reference) {
      setMessage(t('common.loadFailed'));
      return;
    }
    try {
      const nextDetail =
        reference.kind === 'isbn'
          ? await v1Api.catalogBookByIsbn(reference.value)
          : await v1Api.catalogBook(reference.value);
      setDetail(nextDetail);
      if (nextDetail.entitled) {
        setLibraryBook(await v1Api.book(nextDetail.book.id).catch(() => null));
      }
    } catch (caught) {
      setMessage(caught instanceof Error ? caught.message : t('common.loadFailed'));
    }
  }, [rawBookId, rawIsbn, t]);

  useDependencyEffect(() => {
    void load();
  }, [load]);

  // Detail routes may be reused by the router. Prepared Files can be large,
  // so never retain them after navigating away from the book that requested
  // them.
  useDependencyEffect(() => {
    preparedFileGeneration.current += 1;
    preparedFiles.current.clear();
    preparingFiles.current.clear();
    setPreparedFileStates({});
  }, [detail?.book.id]);

  if (!detail) {
    return <p className="py-10 text-sm opacity-65">{message ?? t('common.loading')}</p>;
  }

  const { book } = detail;
  const presentation = presentBookDetail(book);
  const personal = detail.personal_status ?? {
    library_added_at: null,
    reading_progress: null,
    available_formats: [],
    file_count: 0,
    kobo_state: null,
  };
  const formats = Array.from(
    new Set(
      [...presentation.formats, ...personal.available_formats]
        .map((format) => format.toUpperCase())
        .filter(Boolean),
    ),
  );
  const seriesLabel = book.series_name
    ? `${book.series_name}${
        book.series_position
          ? ` · ${t('catalogDetail.seriesPart', { position: book.series_position })}`
          : ''
      }`
    : null;
  let status = {
    label: t('catalogDetail.statusNotAdded'),
    tone: 'bg-slate-600 text-white',
  };
  if (detail.request) {
    status = {
      label: t('catalogDetail.statusRequested'),
      tone: 'bg-violet-600 text-white',
    };
  }
  if (detail.in_library) {
    status = {
      label: t('catalogDetail.statusInLibrary'),
      tone: 'bg-sky-500 text-white',
    };
  }
  if (detail.entitled) {
    status = {
      label: t('catalogDetail.statusAvailable'),
      tone: 'bg-emerald-500 text-white',
    };
  }
  const progress = personal.reading_progress
    ? Math.max(0, Math.min(100, personal.reading_progress.progress))
    : null;
  const requestLabel = detail.request ? t(REQUEST_LABELS[detail.request.status]) : null;
  const requestBlocked = blocksNewRequest(detail.request?.status);
  const requestTone = requestStatusKind(detail.request?.status);
  const libraryDate = formatDate(personal.library_added_at, language);
  const requestDate = formatDate(detail.request?.updated_at, language);
  let koboLabel = t('catalogDetail.koboNotSynced');
  if (personal.kobo_state === 'active') koboLabel = t('catalogDetail.koboActive');
  if (personal.kobo_state === 'deleted') koboLabel = t('catalogDetail.koboDeleted');

  const primaryAction = catalogPrimaryAction(detail, canAddToLibrary);
  const readableFile = libraryBook?.files.find((file) => canReadInBrowser(file.file_format));
  const readerPath = readableFile ? `/library/${book.id}/read/${readableFile.id}` : '/library';
  let openLabel = t('catalogDetail.openPersonal');
  if (readableFile) openLabel = t('catalogDetail.readNow');
  if (progress !== null) openLabel = t('catalogDetail.continueReading');
  const prepareFileForShare = (file: NonNullable<typeof libraryBook>['files'][number]): void => {
    if (preparedFiles.current.has(file.id) || preparingFiles.current.has(file.id)) return;
    const generation = preparedFileGeneration.current;
    // A detail page can expose many formats. Keep exactly one large prepared
    // File; hovering a list must not turn into an unbounded in-memory cache.
    preparedFiles.current.clear();
    preparingFiles.current.clear();
    preparingFiles.current.add(file.id);
    setPreparedFileStates({ [file.id]: 'preparing' });
    void fetchBookFile(v1Api.libraryFileDownloadUrl(book.id, file.id), book.title, file.file_format)
      .then(
        (preparedFile) => {
          if (generation !== preparedFileGeneration.current) return;
          preparedFiles.current.clear();
          preparedFiles.current.set(file.id, preparedFile);
          setPreparedFileStates({ [file.id]: 'ready' });
        },
        () => {
          if (generation !== preparedFileGeneration.current) return;
          setPreparedFileStates({ [file.id]: 'failed' });
        },
      )
      .finally(() => {
        if (generation === preparedFileGeneration.current) preparingFiles.current.delete(file.id);
      });
  };
  const runFileAction = (
    action: 'download' | 'share',
    file: NonNullable<typeof libraryBook>['files'][number],
  ) => {
    if (action === 'share') {
      const preparedFile = preparedFiles.current.get(file.id);
      if (!preparedFile) {
        prepareFileForShare(file);
        setMessage(
          preparedFileStates[file.id] === 'failed'
            ? t('catalogDetail.sharePreparationFailed')
            : t('catalogDetail.sharePreparationPending'),
        );
        return;
      }
      setFileAction(`share-${file.id}`);
      setMessage(null);
      void shareBookFile(preparedFile, book.title)
        .catch((caught: unknown) => {
          if (caught instanceof DOMException && caught.name === 'AbortError') return;
          setMessage(caught instanceof Error ? caught.message : t('common.loadFailed'));
        })
        .finally(() => setFileAction(null));
      return;
    }
    const actionKey = `${action}-${file.id}`;
    setFileAction(actionKey);
    setMessage(null);
    void fetchBookFile(v1Api.libraryFileDownloadUrl(book.id, file.id), book.title, file.file_format)
      .then((bookFile) => downloadBookFile(bookFile))
      .catch((caught: unknown) => {
        if (caught instanceof DOMException && caught.name === 'AbortError') return;
        setMessage(caught instanceof Error ? caught.message : t('common.loadFailed'));
      })
      .finally(() => setFileAction(null));
  };
  let action: ReactNode;
  if (primaryAction === 'open-library') {
    action = (
      <>
        <Link
          to={readerPath}
          className="rounded-lg bg-violet-600 px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-violet-950/30 transition hover:bg-violet-500"
        >
          {openLabel}
        </Link>
        {detail.in_library &&
          (confirmRemoval ? (
            <span className="flex items-center gap-2 rounded-lg border border-amber-200/35 px-3 py-1.5 text-sm text-amber-100">
              {t('catalogDetail.removeLibraryOnly')}
              <button
                type="button"
                disabled={removing}
                className="font-semibold underline disabled:opacity-50"
                onClick={() => {
                  setRemoving(true);
                  void removeLibraryBook(book.id)
                    .then(() => {
                      setMessage(t('catalogDetail.removedFromLibrary'));
                      return load();
                    })
                    .catch((caught: unknown) =>
                      setMessage(caught instanceof Error ? caught.message : t('common.loadFailed')),
                    )
                    .finally(() => setRemoving(false));
                }}
              >
                {removing ? t('catalogDetail.removing') : t('catalogDetail.confirm')}
              </button>
              <button
                type="button"
                disabled={removing}
                className="underline"
                onClick={() => setConfirmRemoval(false)}
              >
                {t('common.cancel')}
              </button>
            </span>
          ) : (
            <button
              type="button"
              className="rounded-lg border border-white/25 px-4 py-2.5 text-sm font-semibold text-white"
              onClick={() => setConfirmRemoval(true)}
            >
              {t('catalogDetail.removeFromLibrary')}
            </button>
          ))}
        {isAdmin &&
          (confirmCatalogDeletion ? (
            <span className="flex items-center gap-2 rounded-lg border border-red-300/45 bg-red-950/30 px-3 py-1.5 text-sm text-red-100">
              <span>
                Verwijder “{book.title}” en alle {libraryBook?.files.length ?? 0} catalogusbestanden
                voor alle gebruikers?
              </span>
              {catalogDeletionPlan && (
                <small>
                  #{catalogDeletionPlan.canonical_book_id}; {catalogDeletionPlan.files.length}{' '}
                  files; IDs: {catalogDeletionPlan.equivalent_book_ids.join(', ')}; memberships{' '}
                  {catalogDeletionPlan.counts.memberships}, entitlements{' '}
                  {catalogDeletionPlan.counts.entitlements}, progress{' '}
                  {catalogDeletionPlan.counts.reading_progress}, requests{' '}
                  {catalogDeletionPlan.counts.requests}.
                </small>
              )}
              <input
                value={catalogDeletionConfirmation}
                onChange={(event) => setCatalogDeletionConfirmation(event.target.value)}
                placeholder={t('catalogDetail.deleteCatalogTyped')}
                className="rounded border border-red-300/45 bg-transparent px-2 py-1 text-sm"
              />
              <button
                type="button"
                disabled={
                  catalogDeletionBusy ||
                  catalogDeletionConfirmation !== (catalogDeletionPlan?.book.title ?? book.title) ||
                  !catalogDeletionToken ||
                  !catalogDeletionPlan ||
                  catalogDeletionPlan.counts.active_processes +
                    catalogDeletionPlan.counts.active_downloads >
                    0
                }
                className="font-semibold underline disabled:opacity-50"
                onClick={() => {
                  setCatalogDeletionBusy(true);
                  void v1Api
                    .deleteCatalogBook(
                      catalogDeletionPlan?.canonical_book_id ?? book.id,
                      catalogDeletionToken ?? '',
                    )
                    .then((result) => {
                      if (result.cleanup.pending > 0 && result.operation_id) {
                        setCatalogCleanupOperationId(result.operation_id);
                        setMessage(t('catalogDetail.cleanupPending'));
                      } else {
                        void navigate('/catalog');
                      }
                      setConfirmCatalogDeletion(false);
                    })
                    .catch((caught: unknown) =>
                      setMessage(caught instanceof Error ? caught.message : t('common.loadFailed')),
                    )
                    .finally(() => setCatalogDeletionBusy(false));
                }}
              >
                {catalogDeletionBusy
                  ? t('catalogDetail.removing')
                  : t('catalogDetail.deleteCatalogConfirm')}
              </button>
              <button
                type="button"
                disabled={catalogDeletionBusy}
                className="underline"
                onClick={() => setConfirmCatalogDeletion(false)}
              >
                {t('common.cancel')}
              </button>
            </span>
          ) : (
            <button
              type="button"
              className="rounded-lg border border-red-300/45 px-4 py-2.5 text-sm font-semibold text-red-200"
              onClick={() => {
                setCatalogDeletionBusy(true);
                void v1Api
                  .catalogDeletionPlan(book.id)
                  .then((result) => {
                    setCatalogDeletionToken(result.plan_token);
                    setCatalogDeletionPlan(result.plan);
                    setCatalogDeletionConfirmation('');
                    setConfirmCatalogDeletion(true);
                  })
                  .catch((caught: unknown) =>
                    setMessage(caught instanceof Error ? caught.message : t('common.loadFailed')),
                  )
                  .finally(() => setCatalogDeletionBusy(false));
              }}
            >
              {t('catalogDetail.deleteCatalog')}
            </button>
          ))}
        {isAdmin && catalogCleanupOperationId && (
          <span className="flex items-center gap-2 rounded-lg border border-amber-300/45 bg-amber-950/30 px-3 py-1.5 text-sm text-amber-100">
            <span>{t('catalogDetail.cleanupPending')}</span>
            <button
              type="button"
              disabled={catalogDeletionBusy}
              className="font-semibold underline disabled:opacity-50"
              onClick={() => {
                setCatalogDeletionBusy(true);
                void v1Api
                  .retryCatalogDeletionCleanup(catalogCleanupOperationId)
                  .then((result) => {
                    if (result.pending === 0) {
                      setCatalogCleanupOperationId(null);
                      void navigate('/catalog');
                    } else {
                      setMessage(t('catalogDetail.cleanupPending'));
                    }
                  })
                  .catch((caught: unknown) =>
                    setMessage(caught instanceof Error ? caught.message : t('common.loadFailed')),
                  )
                  .finally(() => setCatalogDeletionBusy(false));
              }}
            >
              {t('catalogDetail.retryCleanup')}
            </button>
          </span>
        )}
      </>
    );
  } else if (primaryAction === 'add-library') {
    action = (
      <button
        type="button"
        disabled={!book.metadata_provider || !book.provider_book_id}
        className="rounded-lg bg-violet-600 px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-violet-950/30 transition hover:bg-violet-500 disabled:opacity-50"
        onClick={() => {
          if (!book.metadata_provider || !book.provider_book_id) return;
          setAddDialogError(null);
          setAddLibraryToKobo(false);
          setKoboEnabledForAdd(false);
          setLoadingAddOptions(true);
          setAddDialogOpen(true);
          void v1Api
            .koboSettings()
            .then((settings) => setKoboEnabledForAdd(settings.enabled))
            .catch(() => setKoboEnabledForAdd(false))
            .finally(() => setLoadingAddOptions(false));
        }}
      >
        {t('catalogDetail.addBook')}
      </button>
    );
  } else {
    action = (
      <button
        type="button"
        disabled={requesting || requestBlocked}
        className="rounded-lg bg-violet-600 px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-violet-950/30 transition hover:bg-violet-500 disabled:opacity-50"
        onClick={() => {
          void v1Api
            .releaseProfiles()
            .then((result) => {
              const active = result.items.filter((profile) => profile.is_active);
              setProfiles(active);
              setProfileId(
                active.find((profile) => profile.is_default)?.id ?? active[0]?.id ?? null,
              );
              setRequestError(null);
              setRequestDialogOpen(true);
            })
            .catch((caught: unknown) =>
              setMessage(
                caught instanceof Error ? caught.message : t('catalogDetail.requestFailed'),
              ),
            );
        }}
      >
        {requestBlocked && requestLabel ? requestLabel : t('catalogDetail.requestBook')}
      </button>
    );
  }

  return (
    <section className="mx-auto w-full max-w-6xl pb-16">
      <Link
        to="/catalog"
        className="inline-flex items-center gap-2 text-sm text-violet-700 transition hover:text-violet-600 dark:text-violet-300 dark:hover:text-violet-200"
      >
        ← {t('catalogDetail.back')}
      </Link>
      <section className="relative mt-5 overflow-hidden rounded-3xl border border-slate-700/60 bg-slate-950 text-white shadow-2xl">
        {book.cover_url && (
          <div
            aria-hidden="true"
            className="absolute -inset-10 scale-110 bg-cover bg-center opacity-25 blur-2xl"
            style={{ backgroundImage: `url("${withBasePath(book.cover_url)}")` }}
          />
        )}
        <div
          aria-hidden="true"
          className="absolute inset-0 bg-[radial-gradient(circle_at_80%_10%,rgba(124,58,237,0.28),transparent_30rem),linear-gradient(90deg,rgba(7,10,18,0.98)_0%,rgba(7,10,18,0.82)_50%,rgba(7,10,18,0.7)_100%)]"
        />

        <div className="relative grid gap-7 p-5 sm:p-7 md:grid-cols-[12rem_minmax(0,1fr)] lg:gap-10 lg:p-10">
          <div className="mx-auto w-40 md:mx-0 md:w-48">
            {book.cover_url ? (
              <img
                src={withBasePath(book.cover_url)}
                alt={t('common.coverOf', { title: book.title })}
                className="aspect-[2/3] w-full rounded-2xl object-cover shadow-2xl ring-1 shadow-black/50 ring-white/15"
              />
            ) : (
              <div className="flex aspect-[2/3] w-full items-center justify-center rounded-2xl bg-linear-to-br from-violet-700 via-indigo-800 to-slate-950 p-5 text-center text-xl font-bold shadow-2xl ring-1 ring-white/15">
                {book.title}
              </div>
            )}
          </div>

          <div className="flex min-w-0 flex-col justify-center">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`rounded-full px-3 py-1 text-xs font-bold ${status.tone}`}>
                {status.label}
              </span>
              {presentation.genres.slice(0, 4).map((genre) => (
                <span
                  key={genre}
                  className="rounded-full border border-white/15 bg-white/8 px-3 py-1 text-xs text-slate-200"
                >
                  {genre}
                </span>
              ))}
            </div>

            <p className="mt-5 text-xs font-semibold tracking-[0.2em] text-violet-300 uppercase">
              {t('catalogDetail.shared')}
            </p>
            <h1 className="mt-2 text-3xl leading-tight font-bold tracking-tight sm:text-4xl lg:text-5xl">
              {book.title}
            </h1>
            {book.subtitle && <p className="mt-2 text-lg text-slate-300">{book.subtitle}</p>}
            <p className="mt-4 text-base font-semibold text-white">
              {book.author || t('common.unknownAuthor')}
            </p>
            {seriesLabel && <p className="mt-1 text-sm text-slate-300">{seriesLabel}</p>}

            <dl className="mt-7 grid grid-cols-2 gap-x-5 gap-y-5 border-y border-white/10 py-5 sm:grid-cols-3 lg:grid-cols-6">
              {book.publish_year && (
                <Fact label={t('catalogDetail.published')} value={book.publish_year} />
              )}
              {book.language && (
                <Fact label={t('catalogDetail.language')} value={book.language.toUpperCase()} />
              )}
              {presentation.pageCount && (
                <Fact label={t('catalogDetail.pages')} value={presentation.pageCount} />
              )}
              {presentation.publisher && (
                <Fact label={t('catalogDetail.publisher')} value={presentation.publisher} />
              )}
              {formats.length > 0 && (
                <Fact label={t('catalogDetail.formats')} value={formats.join(' · ')} />
              )}
              {presentation.isbn && (
                <Fact label={t('catalogDetail.isbn')} value={presentation.isbn} />
              )}
            </dl>

            <div className="mt-6 flex flex-wrap items-center gap-3">
              {action}
              {message && <p className="text-sm text-slate-300">{message}</p>}
            </div>
          </div>
        </div>
      </section>
      {addDialogOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="add-library-heading"
        >
          <form
            className="w-full max-w-md rounded-xl bg-(--bg) p-5 shadow-2xl"
            onSubmit={(event) => {
              event.preventDefault();
              const metadataProvider = book.metadata_provider;
              const providerBookId = book.provider_book_id;
              if (!metadataProvider || !providerBookId) return;
              setAdding(true);
              setAddDialogError(null);
              void (async () => {
                let addedToLibrary = false;
                try {
                  const result = await addLibraryBook(metadataProvider, providerBookId);
                  addedToLibrary = true;
                  if (addLibraryToKobo) {
                    const persisted = await v1Api.book(result.book_id);
                    const file = persisted.files[0];
                    if (!file) throw new Error(t('catalogDetail.addKoboNoFile'));
                    await v1Api.addKoboBook(
                      result.book_id,
                      file.id,
                      file.file_format.toLowerCase(),
                    );
                  }
                  setAddDialogOpen(false);
                  await load();
                  setMessage(t('catalogDetail.added'));
                } catch (caught) {
                  const failure =
                    caught instanceof Error ? caught.message : t('catalogDetail.addFailed');
                  if (addedToLibrary) {
                    setAddDialogOpen(false);
                    await load();
                    setMessage(t('catalogDetail.addedKoboFailed', { error: failure }));
                  } else {
                    setAddDialogError(failure);
                  }
                } finally {
                  setAdding(false);
                }
              })();
            }}
          >
            <h2 id="add-library-heading" className="text-lg font-semibold">
              {t('catalogDetail.addConfirmTitle')}
            </h2>
            <p className="mt-2 text-sm opacity-65">
              {t('catalogDetail.addConfirmDescription', { title: book.title })}
            </p>
            {loadingAddOptions && (
              <p className="mt-4 text-sm opacity-60">{t('catalogDetail.checkingKobo')}</p>
            )}
            {!loadingAddOptions && koboEnabledForAdd && (
              <label className="mt-4 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={addLibraryToKobo}
                  onChange={(event) => setAddLibraryToKobo(event.target.checked)}
                />
                {t('catalogDetail.addToKobo')}
              </label>
            )}
            {addDialogError && (
              <p
                role="alert"
                className="mt-4 rounded-md border border-red-400/35 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-200"
              >
                {addDialogError}
              </p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                disabled={adding}
                className="rounded border border-(--border-muted) px-3 py-2 text-sm"
                onClick={() => setAddDialogOpen(false)}
              >
                {t('common.cancel')}
              </button>
              <button
                type="submit"
                disabled={adding || loadingAddOptions}
                className="rounded bg-violet-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
              >
                {adding ? t('catalogDetail.adding') : t('catalogDetail.confirm')}
              </button>
            </div>
          </form>
        </div>
      )}
      {requestDialogOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="request-book-heading"
        >
          <form
            className="w-full max-w-md rounded-xl bg-(--bg) p-5 shadow-2xl"
            onSubmit={(event) => {
              event.preventDefault();
              if (!profileId) return;
              setRequesting(true);
              setRequestError(null);
              void v1Api
                .createRequest(
                  {
                    book_id: book.id,
                    profile_id: profileId,
                    content_type: 'ebook',
                    add_to_kobo_when_available: addToKobo,
                  },
                  createIdempotencyKey(),
                )
                .then(() => {
                  setRequestDialogOpen(false);
                  setMessage(t('catalogDetail.requestCreated'));
                  return load();
                })
                .catch((caught: unknown) =>
                  setRequestError(formatRequestError(caught, t('catalogDetail.requestFailed'))),
                )
                .finally(() => setRequesting(false));
            }}
          >
            <h2 id="request-book-heading" className="text-lg font-semibold">
              Request {book.title}
            </h2>
            <p className="mt-2 text-sm opacity-65">
              Choose the release profile used to find this book.
            </p>
            <label className="mt-4 block text-sm font-medium">
              Release profile
              <select
                className="mt-1 w-full rounded-md border border-(--border-muted) bg-transparent px-3 py-2"
                value={profileId ?? ''}
                onChange={(event) => setProfileId(Number(event.target.value))}
                required
              >
                {profiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.name}
                    {profile.is_default ? ' (default)' : ''}
                  </option>
                ))}
              </select>
            </label>
            {profiles.length === 0 && (
              <p className="mt-3 text-sm text-red-600">
                No active release profiles are available. Ask an administrator to create one.
              </p>
            )}
            {requestError && (
              <p
                role="alert"
                className="mt-3 rounded-md border border-red-400/35 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-200"
              >
                {requestError}
              </p>
            )}
            <label className="mt-4 flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={addToKobo}
                onChange={(event) => setAddToKobo(event.target.checked)}
              />{' '}
              Add to Kobo Sync when available
            </label>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                className="rounded border border-(--border-muted) px-3 py-2 text-sm"
                onClick={() => setRequestDialogOpen(false)}
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={requesting || !profileId}
                className="rounded bg-violet-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
              >
                {requesting ? 'Requesting…' : 'Request book'}
              </button>
            </div>
          </form>
        </div>
      )}

      <div className="mt-8 grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <article className="min-w-0">
          <h2 className="text-xl font-semibold">{t('catalogDetail.description')}</h2>
          <p className="mt-4 leading-7 whitespace-pre-line opacity-75">
            {presentation.description || t('catalogDetail.noDescription')}
          </p>
        </article>

        <aside className="bookflow-panel rounded-2xl p-5" aria-labelledby="personal-book-heading">
          <h2 id="personal-book-heading" className="text-lg font-semibold">
            {t('catalogDetail.personalTitle')}
          </h2>
          <p className="mt-1 text-sm opacity-60">{t('catalogDetail.personalHelp')}</p>

          {libraryBook && libraryBook.files.length > 0 && (
            <div className="mt-5 space-y-2" aria-label={t('catalogDetail.availableFiles')}>
              {libraryBook.files.map((file) => {
                const readable = canReadInBrowser(file.file_format);
                let shareLabel = t('catalogDetail.openOtherApp');
                if (fileAction === `share-${file.id}`) {
                  shareLabel = t('catalogDetail.openingOtherApp');
                }
                if (preparedFileStates[file.id] === 'preparing') {
                  shareLabel = t('catalogDetail.preparingFile');
                }
                return (
                  <div
                    key={file.id}
                    className="rounded-xl border border-(--border-muted) bg-(--bg) p-3"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="font-semibold">{file.file_format.toUpperCase()}</span>
                      <span className="text-xs opacity-60">
                        {(file.size_bytes / 1024 / 1024).toFixed(1)} MB
                      </span>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {readable && (
                        <Link
                          to={`/library/${book.id}/read/${file.id}`}
                          className="rounded-md bg-violet-600 px-2.5 py-1.5 text-xs font-semibold text-white"
                        >
                          {t('catalogDetail.readNow')}
                        </Link>
                      )}
                      <button
                        type="button"
                        disabled={fileAction !== null}
                        className="rounded-md border border-(--border-muted) px-2.5 py-1.5 text-xs font-semibold disabled:opacity-50"
                        onClick={() => runFileAction('download', file)}
                      >
                        {fileAction === `download-${file.id}`
                          ? t('catalogDetail.downloading')
                          : t('bookDetail.download')}
                      </button>
                      <button
                        type="button"
                        disabled={fileAction !== null}
                        className="rounded-md border border-(--border-muted) px-2.5 py-1.5 text-xs font-semibold disabled:opacity-50"
                        onPointerEnter={() => prepareFileForShare(file)}
                        onFocus={() => prepareFileForShare(file)}
                        onClick={() => runFileAction('share', file)}
                      >
                        {shareLabel}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {progress !== null && (
            <div className="mt-5 rounded-xl border border-(--border-muted) bg-(--bg) p-4">
              <div className="flex items-center justify-between gap-3 text-sm">
                <span className="font-medium">{t('catalogDetail.readingProgress')}</span>
                <span className="font-bold text-violet-500">{Math.round(progress)}%</span>
              </div>
              <div className="mt-3 h-2 overflow-hidden rounded-full bg-(--border-muted)">
                <div
                  className="h-full rounded-full bg-linear-to-r from-violet-600 to-sky-400"
                  style={{ width: `${progress}%` }}
                />
              </div>
              <Link
                to={readerPath}
                className="mt-4 inline-flex text-sm font-semibold text-violet-600 hover:text-violet-500 dark:text-violet-300"
              >
                {t('catalogDetail.continueReading')} →
              </Link>
            </div>
          )}

          <dl className="mt-5 divide-y divide-(--border-muted) text-sm">
            <div className="flex items-start justify-between gap-4 py-3">
              <dt className="opacity-60">{t('catalogDetail.catalogAvailability')}</dt>
              <dd className="text-right font-medium">
                {detail.delivery_state === 'available' ? (
                  <>
                    <span>{t('catalogDetail.available')}</span>
                    {detail.available_formats.length > 0 && (
                      <span className="mt-1 block text-xs opacity-60">
                        {detail.available_formats.map((format) => format.toUpperCase()).join(' · ')}
                      </span>
                    )}
                  </>
                ) : (
                  t('catalogDetail.notAvailable')
                )}
              </dd>
            </div>
            {libraryDate && (
              <div className="flex items-start justify-between gap-4 py-3">
                <dt className="opacity-60">{t('catalogDetail.addedOn')}</dt>
                <dd className="text-right font-medium">{libraryDate}</dd>
              </div>
            )}
            {detail.request && requestLabel && (
              <div className="flex items-start justify-between gap-4 py-3">
                <dt className="opacity-60">{t('catalogDetail.requestStatus')}</dt>
                <dd className="text-right">
                  <span
                    className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-semibold ${REQUEST_TONE_CLASSES[requestTone]}`}
                  >
                    {requestLabel}
                  </span>
                  {requestDate && (
                    <span className="mt-1 block text-xs opacity-55">
                      {t('catalogDetail.requestUpdated')}: {requestDate}
                    </span>
                  )}
                </dd>
              </div>
            )}
            <div className="flex items-start justify-between gap-4 py-3">
              <dt className="opacity-60">{t('catalogDetail.availableFiles')}</dt>
              <dd className="text-right font-medium">
                {personal.file_count > 0 ? (
                  <>
                    <span>{t('catalogDetail.fileCount', { count: personal.file_count })}</span>
                    <span className="mt-1 block text-xs opacity-60">
                      {personal.available_formats.map((format) => format.toUpperCase()).join(' · ')}
                    </span>
                  </>
                ) : (
                  t('catalogDetail.noFormats')
                )}
              </dd>
            </div>
            <div className="flex items-start justify-between gap-4 py-3">
              <dt className="opacity-60">{t('catalogDetail.kobo')}</dt>
              <dd className="text-right font-medium">{koboLabel}</dd>
            </div>
          </dl>
        </aside>
      </div>

      <section className="bookflow-panel mt-10 rounded-2xl p-5">
        <h2 className="font-semibold">{t('catalogDetail.reportTitle')}</h2>
        <p className="mt-1 text-sm opacity-65">{t('catalogDetail.reportHelp')}</p>
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <select
            value={reportField}
            onChange={(event) => setReportField(event.target.value)}
            className="rounded border border-(--border-muted) bg-transparent px-3 py-2 text-sm"
          >
            <option value="title">{t('metadata.field.title')}</option>
            <option value="author">{t('metadata.field.author')}</option>
            <option value="series_name">{t('metadata.field.series')}</option>
            <option value="description">{t('metadata.field.description')}</option>
            <option value="cover_url">{t('metadata.field.cover')}</option>
          </select>
          <input
            value={reportNote}
            onChange={(event) => setReportNote(event.target.value)}
            className="min-w-0 flex-1 rounded border border-(--border-muted) bg-transparent px-3 py-2 text-sm"
            placeholder={t('catalogDetail.reportPlaceholder')}
          />
          <button
            type="button"
            disabled={!reportNote.trim()}
            className="rounded border border-(--border-muted) px-3 py-2 text-sm disabled:opacity-50"
            onClick={() => {
              void v1Api
                .createMetadataReport(book.id, reportField, reportNote)
                .then(() => {
                  setReportNote('');
                  setMessage(t('catalogDetail.reportSent'));
                })
                .catch((caught: unknown) =>
                  setMessage(
                    caught instanceof Error ? caught.message : t('catalogDetail.reportFailed'),
                  ),
                );
            }}
          >
            {t('catalogDetail.report')}
          </button>
        </div>
      </section>
    </section>
  );
};
