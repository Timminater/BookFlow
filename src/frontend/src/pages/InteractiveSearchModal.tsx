import { useCallback, useMemo, useRef, useState, type KeyboardEvent } from 'react';

import { useLanguage } from '../contexts/LanguageContext';
import { useDependencyEffect } from '../hooks/useMountEffect';
import { v1Api } from '../services/v1Api';
import type { V1Attempt, V1Candidate, V1RequestProcess, V1RequestSearchRun } from '../types/v1';
import {
  filterInteractiveCandidates,
  sortInteractiveCandidates,
  toInteractiveCandidate,
  type InteractiveSearchSort,
} from './interactiveSearchModel';

interface InteractiveSearchModalProps {
  requestProcess: V1RequestProcess;
  title: string;
  author?: string | null;
  profile?: string | null;
  onClose: () => void;
  onShowToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onChanged: () => Promise<void>;
}

const terminalStatuses = new Set<V1RequestSearchRun['status']>(['completed', 'partial', 'failed']);
const isRejected = (candidate: V1Candidate): boolean =>
  candidate.accepted !== 1 || candidate.blocked === 1;
const toInteractiveSearchSort = (value: string): InteractiveSearchSort =>
  value === 'title' || value === 'seeders' || value === 'size' ? value : 'score';
const errorMessage = (caught: unknown, fallback: string): string =>
  caught instanceof Error ? caught.message : fallback;
const runErrors = (run: V1RequestSearchRun | null): string[] => {
  if (!run?.errors_json) return [];
  try {
    const parsed: unknown = JSON.parse(run.errors_json);
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      return Object.values(parsed).map(String);
    }
    return [String(parsed)];
  } catch {
    return [run.errors_json];
  }
};

export const InteractiveSearchModal = ({
  requestProcess,
  title,
  author,
  profile,
  onClose,
  onShowToast,
  onChanged,
}: InteractiveSearchModalProps) => {
  const { t } = useLanguage();
  const [candidates, setCandidates] = useState<V1Candidate[]>([]);
  const [attempts, setAttempts] = useState<V1Attempt[]>([]);
  const [searchRun, setSearchRun] = useState<V1RequestSearchRun | null>(null);
  const [sort, setSort] = useState<InteractiveSearchSort>('score');
  const [source, setSource] = useState('all');
  const [format, setFormat] = useState('all');
  const [language, setLanguage] = useState('all');
  const [searching, setSearching] = useState(true);
  const [loadingResults, setLoadingResults] = useState(false);
  const [showAttempts, setShowAttempts] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollCount = useRef(0);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());
  const previousFocus = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const clearPoll = () => {
    if (pollTimer.current) {
      clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  };
  useDependencyEffect(() => {
    mounted.current = true;
    previousFocus.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || !dialogRef.current) return;
      const focusable = [
        ...dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not(:disabled), select:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
        ),
      ];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      mounted.current = false;
      clearPoll();
      document.removeEventListener('keydown', handleKeyDown);
      previousFocus.current?.focus();
    };
  }, []);

  const refreshRun = useCallback(
    async (runId: number) => {
      setLoadingResults(true);
      try {
        const [candidatePage, attemptPage, runResponse] = await Promise.all([
          v1Api.candidates(requestProcess.id, runId),
          v1Api.attempts(requestProcess.id),
          v1Api.requestSearchRun(requestProcess.id, runId),
        ]);
        if (!mounted.current) return null;
        setCandidates(candidatePage.items);
        setAttempts(attemptPage.items);
        setSearchRun(runResponse.search_run);
        setSearching(!terminalStatuses.has(runResponse.search_run.status));
        setError(null);
        return runResponse.search_run;
      } catch (caught) {
        if (mounted.current) setError(errorMessage(caught, t('interactiveSearch.failed')));
        return null;
      } finally {
        if (mounted.current) setLoadingResults(false);
      }
    },
    [requestProcess.id, t],
  );

  const startSearch = useCallback(async () => {
    clearPoll();
    pollCount.current = 0;
    setSearching(true);
    setError(null);
    setCandidates([]);
    setAttempts([]);
    setSearchRun(null);
    try {
      const response = await v1Api.interactiveSearch(requestProcess.id);
      if (!mounted.current) return;
      await refreshRun(response.search_run.id);
    } catch (caught) {
      if (mounted.current) {
        setError(errorMessage(caught, t('interactiveSearch.failed')));
        setSearching(false);
      }
    }
  }, [refreshRun, requestProcess.id, t]);
  useDependencyEffect(() => {
    void startSearch();
  }, [startSearch]);
  useDependencyEffect(() => {
    if (!searchRun || terminalStatuses.has(searchRun.status)) return;
    pollCount.current += 1;
    if (pollCount.current > 80) {
      setSearching(false);
      setError(t('interactiveSearch.timeout'));
      return;
    }
    clearPoll();
    pollTimer.current = setTimeout(() => void refreshRun(searchRun.id), 750);
  }, [refreshRun, searchRun, t]);

  const grab = async (candidate: V1Candidate) => {
    const force = isRejected(candidate);
    let reason: string | undefined;
    if (force) {
      if (
        // oxlint-disable-next-line no-alert -- a forced profile override needs deliberate admin confirmation.
        !window.confirm(t('interactiveSearch.forceConfirm'))
      )
        return;
      // oxlint-disable-next-line no-alert -- the backend persists this reason in the override audit.
      reason = window.prompt(t('interactiveSearch.auditReason'))?.trim();
      if (!reason) return;
    }
    try {
      await v1Api.selectCandidate(candidate.id, force, reason);
      onShowToast(
        force ? t('interactiveSearch.forceSuccess') : t('interactiveSearch.grabSuccess'),
        'success',
      );
      await Promise.all([searchRun ? refreshRun(searchRun.id) : Promise.resolve(), onChanged()]);
    } catch (caught) {
      setError(errorMessage(caught, t('interactiveSearch.failed')));
    }
  };

  const presented = useMemo(
    () =>
      sortInteractiveCandidates(candidates, sort).map((candidate) =>
        toInteractiveCandidate(candidate, {
          unknown: t('common.unknown'),
          unknownSource: t('interactiveSearch.unknownSource'),
        }),
      ),
    [candidates, sort, t],
  );
  const sources = useMemo(
    () => [...new Set(presented.map((candidate) => candidate.source))],
    [presented],
  );
  const formats = useMemo(
    () => [...new Set(presented.map((candidate) => candidate.format))],
    [presented],
  );
  const languages = useMemo(
    () => [...new Set(presented.map((candidate) => candidate.language))],
    [presented],
  );
  const ordered = useMemo(
    () => filterInteractiveCandidates(presented, source, format, language),
    [presented, source, format, language],
  );
  const errors = runErrors(searchRun);
  const sourceCount = (item: string): number =>
    item === 'all'
      ? presented.length
      : presented.filter((candidate) => candidate.source === item).length;
  const decisionLabel = (decision: 'eligible' | 'rejected' | 'blocked'): string => {
    if (decision === 'eligible') return `✓ ${t('interactiveSearch.eligible')}`;
    if (decision === 'blocked') return `⛔ ${t('interactiveSearch.blocked')}`;
    return `⚠ ${t('interactiveSearch.rejected')}`;
  };
  const actionLabel = (candidate: V1Candidate): string => {
    if (candidate.selected_by_admin === 1) return t('interactiveSearch.selected');
    return isRejected(candidate)
      ? t('interactiveSearch.grabOverride')
      : t('interactiveSearch.grab');
  };
  const runStatusLabel = (status: V1RequestSearchRun['status']): string => {
    if (status === 'queued') return t('interactiveSearch.status.queued');
    if (status === 'running') return t('interactiveSearch.status.running');
    if (status === 'completed') return t('interactiveSearch.status.completed');
    if (status === 'partial') return t('interactiveSearch.status.partial');
    return t('interactiveSearch.status.failed');
  };
  const attemptStatusLabel = (status: string): string => {
    if (status === 'queued') return t('interactiveSearch.attempt.queued');
    if (status === 'submitted') return t('interactiveSearch.attempt.submitted');
    if (status === 'downloading') return t('interactiveSearch.attempt.downloading');
    if (status === 'importing') return t('interactiveSearch.attempt.importing');
    if (status === 'completed') return t('interactiveSearch.attempt.completed');
    if (status === 'retry_scheduled') return t('interactiveSearch.attempt.retryScheduled');
    if (status === 'failed') return t('interactiveSearch.attempt.failed');
    if (status === 'cancelled') return t('interactiveSearch.attempt.cancelled');
    return t('common.unknown');
  };
  const tabs = ['all', ...sources];
  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, item: string) => {
    const currentIndex = tabs.indexOf(item);
    let nextIndex: number | null = null;
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = tabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const next = tabs[nextIndex];
    setSource(next);
    tabRefs.current.get(next)?.focus();
  };
  const controlClass = 'min-h-11 rounded-md border border-(--border-muted) bg-(--bg) px-3 text-sm';
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-0 sm:p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby="interactive-search-heading"
      ref={dialogRef}
    >
      <section className="min-h-full w-full max-w-6xl bg-(--bg) shadow-2xl sm:min-h-0 sm:rounded-2xl">
        <header className="flex items-start justify-between gap-4 border-b border-(--border-muted) p-5">
          <div className="flex min-w-0 items-center gap-4">
            <div
              className="flex h-24 w-16 shrink-0 items-center justify-center rounded-lg bg-linear-to-br from-violet-500/25 to-fuchsia-500/10 px-2 text-center text-[10px] font-semibold uppercase"
              aria-label={t('interactiveSearch.noCover')}
            >
              {t('interactiveSearch.noCover')}
            </div>
            <div className="min-w-0">
              <p className="text-xs font-semibold tracking-widest text-violet-600 uppercase dark:text-violet-300">
                {t('interactiveSearch.title')}
              </p>
              <h2 id="interactive-search-heading" className="mt-1 truncate text-2xl font-semibold">
                {title}
              </h2>
              {author && <p className="mt-1 text-sm opacity-70">{author}</p>}
              <div className="mt-2 flex flex-wrap gap-2 text-xs">
                <span className="rounded-full bg-(--bg-soft) px-2.5 py-1">
                  {t('interactiveSearch.profile')}:{' '}
                  {profile ?? t('interactiveSearch.defaultProfile')}
                </span>
                <span className="rounded-full bg-(--bg-soft) px-2.5 py-1">
                  {searchRun ? runStatusLabel(searchRun.status) : t('interactiveSearch.starting')} ·{' '}
                  {candidates.length}
                </span>
              </div>
            </div>
          </div>
          <button
            type="button"
            ref={closeButtonRef}
            className="min-h-11 min-w-11 rounded-full text-xl hover:bg-(--hover-surface)"
            onClick={onClose}
            aria-label={t('interactiveSearch.close')}
          >
            ×
          </button>
        </header>
        <div
          className="flex gap-1 overflow-x-auto border-b border-(--border-muted) px-4 pt-2"
          role="tablist"
          aria-label={t('interactiveSearch.sources')}
        >
          {tabs.map((item) => (
            <button
              key={item}
              type="button"
              role="tab"
              aria-selected={source === item}
              aria-controls="interactive-search-results"
              tabIndex={source === item ? 0 : -1}
              ref={(element) => {
                if (element) tabRefs.current.set(item, element);
                else tabRefs.current.delete(item);
              }}
              className={`min-h-11 shrink-0 border-b-2 px-4 text-sm font-medium ${source === item ? 'border-violet-500 text-violet-600 dark:text-violet-300' : 'border-transparent opacity-65'}`}
              onClick={() => setSource(item)}
              onKeyDown={(event) => handleTabKeyDown(event, item)}
            >
              {item === 'all' ? t('interactiveSearch.allSources') : item} ({sourceCount(item)})
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-(--border-muted) px-5 py-3">
          <p className="text-sm">
            {searching
              ? t('interactiveSearch.searching')
              : t('interactiveSearch.results', { count: ordered.length })}
            {searchRun ? ` · ${runStatusLabel(searchRun.status)}` : ''}
          </p>
          <div className="flex flex-wrap gap-2">
            <select
              aria-label={t('interactiveSearch.filterFormat')}
              value={format}
              onChange={(event) => setFormat(event.target.value)}
              className={controlClass}
            >
              <option value="all">{t('interactiveSearch.allFormats')}</option>
              {formats.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
            <select
              aria-label={t('interactiveSearch.filterLanguage')}
              value={language}
              onChange={(event) => setLanguage(event.target.value)}
              className={controlClass}
            >
              <option value="all">{t('interactiveSearch.allLanguages')}</option>
              {languages.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
            <select
              aria-label={t('interactiveSearch.sort')}
              value={sort}
              onChange={(event) => setSort(toInteractiveSearchSort(event.target.value))}
              className={controlClass}
            >
              <option value="score">{t('interactiveSearch.sortScore')}</option>
              <option value="seeders">{t('interactiveSearch.sortPeers')}</option>
              <option value="size">{t('interactiveSearch.sortSize')}</option>
              <option value="title">{t('interactiveSearch.sortTitle')}</option>
            </select>
            <button
              type="button"
              className={controlClass}
              disabled={searching || loadingResults}
              onClick={() => void startSearch()}
            >
              {t('interactiveSearch.searchAgain')}
            </button>
            <button
              type="button"
              className={controlClass}
              disabled={loadingResults || !searchRun}
              onClick={() => searchRun && void refreshRun(searchRun.id)}
            >
              {t('interactiveSearch.refresh')}
            </button>
          </div>
        </div>
        {error && (
          <p className="mx-5 mt-4 rounded border border-red-400/35 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-200">
            {error}
          </p>
        )}
        {errors.length > 0 && (
          <p className="mx-5 mt-4 rounded border border-amber-400/35 bg-amber-500/10 p-3 text-sm text-amber-800 dark:text-amber-200">
            {errors.join(' · ')}
          </p>
        )}
        <div id="interactive-search-results" role="tabpanel" className="space-y-3 p-3 sm:p-5">
          {ordered.map((item) => (
            <article
              key={item.id}
              className="grid gap-3 rounded-xl border border-(--border-muted) p-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center"
            >
              <div className="min-w-0">
                <h3 className="font-semibold break-words">{item.title}</h3>
                <p className="mt-1 text-xs opacity-65">
                  {item.source}
                  {item.indexer ? ` · ${item.indexer}` : ''} · {item.protocol}
                </p>
                <div className="mt-3 flex flex-wrap gap-2 text-xs">
                  <span className="rounded-lg bg-blue-500/15 px-2 py-1 font-semibold">
                    {item.language}
                  </span>
                  <span className="rounded-lg bg-emerald-500/15 px-2 py-1 font-semibold">
                    {item.format}
                  </span>
                  <span className="rounded-lg bg-(--bg-soft) px-2 py-1">{item.size}</span>
                  <span className="rounded-lg bg-(--bg-soft) px-2 py-1">
                    {item.seeders} / {item.peers} {t('interactiveSearch.peers')}
                  </span>
                  <span className="rounded-lg bg-violet-500/15 px-2 py-1 font-semibold">
                    {t('interactiveSearch.score')} {item.score}
                  </span>
                </div>
                <p
                  className={`mt-3 text-sm font-medium ${item.decision === 'eligible' ? 'text-emerald-700 dark:text-emerald-300' : 'text-amber-700 dark:text-amber-300'}`}
                >
                  {decisionLabel(item.decision)}
                </p>
                {item.rejectReasons.length > 0 && (
                  <ul className="mt-1 list-disc pl-5 text-xs opacity-75">
                    {item.rejectReasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                )}
              </div>
              <button
                type="button"
                className="min-h-11 w-full rounded-lg bg-violet-600 px-4 text-sm font-semibold text-white disabled:opacity-50 sm:w-auto"
                onClick={() => void grab(item.candidate)}
                disabled={item.selected}
              >
                {actionLabel(item.candidate)}
              </button>
            </article>
          ))}
          {!searching && ordered.length === 0 && (
            <p className="py-8 text-center text-sm opacity-65">
              {t('interactiveSearch.noResults')}
            </p>
          )}
        </div>
        <footer className="border-t border-(--border-muted) px-5 py-4">
          <button
            type="button"
            className="min-h-11 text-sm font-medium underline"
            aria-expanded={showAttempts}
            onClick={() => setShowAttempts((current) => !current)}
          >
            {showAttempts
              ? t('interactiveSearch.hideAttempts', { count: attempts.length })
              : t('interactiveSearch.showAttempts', { count: attempts.length })}
          </button>
          {showAttempts && (
            <div className="mt-3 space-y-1 text-sm">
              {attempts.length === 0 ? (
                <p className="opacity-65">{t('interactiveSearch.noAttempts')}</p>
              ) : (
                attempts.map((attempt) => (
                  <p key={attempt.id}>
                    #{attempt.attempt_number} · {attemptStatusLabel(attempt.status)}
                    {attempt.error_message ? ` · ${attempt.error_message}` : ''}
                  </p>
                ))
              )}
            </div>
          )}
        </footer>
      </section>
    </div>
  );
};
