import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';

import { useLanguage } from '../contexts/LanguageContext';
import { useDependencyEffect } from '../hooks/useMountEffect';
import { v1Api } from '../services/v1Api';
import type { V1AdminRequest, V1RequestProcess, V1UserRequest } from '../types/v1';
import { createIdempotencyKey } from '../utils/idempotencyKey';
import { InteractiveSearchModal } from './InteractiveSearchModal';
import { canManageRequestProcess, requesterName, requesterSummary } from './requestsPresentation';

interface RequestsPageProps {
  isAdmin: boolean;
  onShowToast: (message: string, type: 'success' | 'error' | 'info') => void;
}

const terminal = new Set(['fulfilled', 'rejected', 'cancelled']);
const errorMessage = (caught: unknown): string =>
  caught instanceof Error ? caught.message : 'The action could not be completed.';

/** Book-centric request list. Admin actions operate on the shared request process. */
export const RequestsPage = ({ isAdmin, onShowToast }: RequestsPageProps) => {
  const { t } = useLanguage();
  const [requests, setRequests] = useState<Array<V1AdminRequest | V1UserRequest>>([]);
  const [requestProcesses, setRequestProcesses] = useState<V1RequestProcess[]>([]);
  const [history, setHistory] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [interactiveSearch, setInteractiveSearch] = useState<{
    requestProcess: V1RequestProcess;
    title: string;
    author?: string | null;
    profile?: string | null;
  } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [requestPage, requestProcessPage] = await Promise.all([
        isAdmin ? v1Api.adminRequests(undefined, 0, 200) : v1Api.userRequests(0, 200),
        isAdmin
          ? v1Api.requestProcesses(0, 200)
          : Promise.resolve({ items: [] as V1RequestProcess[] }),
      ]);
      setRequests(requestPage.items);
      setRequestProcesses(requestProcessPage.items);
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [isAdmin]);
  useDependencyEffect(() => {
    void load();
  }, [load]);

  const action = async (operation: () => Promise<unknown>, success: string) => {
    try {
      await operation();
      onShowToast(success, 'success');
      await load();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };
  const rejectRequest = (requestId: number) => {
    // oxlint-disable-next-line no-alert -- optional reviewer notes belong on the rejection audit.
    const note = window.prompt('Optional rejection note') ?? undefined;
    void action(() => v1Api.rejectRequest(requestId, note), 'Request rejected');
  };

  const visible = requests.filter((request) =>
    history ? terminal.has(request.status) : !terminal.has(request.status),
  );
  const groups = new Map<number, Array<V1AdminRequest | V1UserRequest>>();
  for (const request of visible)
    groups.set(request.book_id, [...(groups.get(request.book_id) ?? []), request]);
  const requestProcessFor = (bookId: number) =>
    requestProcesses.find((item) => item.book_id === bookId);

  return (
    <section className="pb-16">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold text-(--text)">Requests</h1>
          <p className="mt-2 text-sm opacity-65">
            {isAdmin
              ? 'Approve requests and manage their shared request process.'
              : 'Track your book requests and cancel them until they are fulfilled.'}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            className="rounded-md border border-(--border-muted) px-3 py-2 text-sm"
            onClick={() => setHistory(false)}
          >
            Active
          </button>
          <button
            type="button"
            className="rounded-md border border-(--border-muted) px-3 py-2 text-sm"
            onClick={() => setHistory(true)}
          >
            History
          </button>
          <button
            type="button"
            className="rounded-md border border-(--border-muted) px-3 py-2 text-sm"
            onClick={() => void load()}
          >
            Refresh
          </button>
        </div>
      </div>
      {error && (
        <p className="mt-5 rounded-md border border-red-400/35 bg-red-500/10 p-3 text-sm text-red-700 dark:text-red-200">
          {error}
        </p>
      )}
      {loading ? (
        <p className="mt-8 text-sm opacity-65">Loading requests…</p>
      ) : (
        [...groups.entries()].map(([bookId, group]) => {
          const first = group[0];
          const requestProcess = requestProcessFor(bookId);
          const title = 'book_title' in first ? first.book_title : first.title;
          const requesterNames = isAdmin
            ? group.map((request) => ('requester_name' in request ? request.requester_name : null))
            : [];
          return (
            <article key={bookId} className="mt-5 rounded-xl border border-(--border-muted) p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="font-semibold">
                    <Link
                      to={`/catalog/${bookId}`}
                      className="transition hover:text-violet-600 hover:underline dark:hover:text-violet-300"
                    >
                      {title || `Book #${bookId}`}
                    </Link>
                  </h2>
                  {'book_author' in first && first.book_author && (
                    <p className="text-sm opacity-65">{first.book_author}</p>
                  )}
                  <p className="mt-1 text-xs opacity-60">
                    {isAdmin
                      ? requesterSummary(requesterNames, {
                          requestedBy: t('requests.requestedBy'),
                          requestedByCount: t('requests.requestedByCount'),
                          unknownRequester: t('requests.unknownRequester'),
                        })
                      : `${group.length} requester${group.length === 1 ? '' : 's'}`}{' '}
                    · profile {first.profile_name ?? 'Default'}
                  </p>
                </div>
                <span className="rounded-full bg-(--bg-soft) px-3 py-1 text-xs font-medium">
                  {requestProcess?.status ?? first.status}
                </span>
              </div>
              <div className="mt-4 flex flex-wrap gap-2">
                {!isAdmin &&
                  (first.status === 'pending_approval' || first.status === 'approved') && (
                    <button
                      type="button"
                      className="rounded border border-red-500/40 px-3 py-1.5 text-xs text-red-700"
                      onClick={() =>
                        void action(
                          () => v1Api.cancelRequest(first.id, createIdempotencyKey()),
                          'Request cancelled',
                        )
                      }
                    >
                      Cancel request
                    </button>
                  )}
                {isAdmin && first.status === 'pending_approval' && (
                  <>
                    <button
                      type="button"
                      className="rounded bg-violet-600 px-3 py-1.5 text-xs font-medium text-white"
                      onClick={() =>
                        void action(
                          () => v1Api.approveRequest(first.id),
                          'All requests for this book were approved',
                        )
                      }
                    >
                      Approve book
                    </button>
                    <button
                      type="button"
                      className="rounded border border-red-500/40 px-3 py-1.5 text-xs text-red-700"
                      onClick={() => rejectRequest(first.id)}
                    >
                      Reject
                    </button>
                  </>
                )}
                {isAdmin &&
                  requestProcess &&
                  canManageRequestProcess(history, requestProcess.status) && (
                    <>
                      <button
                        type="button"
                        className="rounded border border-(--border-muted) px-3 py-1.5 text-xs"
                        onClick={() =>
                          void action(
                            () => v1Api.searchNow(requestProcess.id),
                            'Automatic search scheduled',
                          )
                        }
                      >
                        Automatic Search
                      </button>
                      <button
                        type="button"
                        className="rounded border border-(--border-muted) px-3 py-1.5 text-xs"
                        onClick={() =>
                          setInteractiveSearch({
                            requestProcess,
                            title: title || `Book #${bookId}`,
                            author: 'book_author' in first ? first.book_author : null,
                            profile: first.profile_name,
                          })
                        }
                      >
                        Interactive Search
                      </button>
                      <button
                        type="button"
                        className="rounded border border-red-500/40 px-3 py-1.5 text-xs text-red-700 dark:text-red-300"
                        onClick={() => {
                          // oxlint-disable-next-line no-alert -- cancelling affects every requester in this process.
                          if (!window.confirm('Cancel this request for all requesters?')) return;
                          void action(
                            () => v1Api.cancelRequestProcess(requestProcess.id),
                            'Request cancelled',
                          );
                        }}
                      >
                        {t('requests.cancelProcess')}
                      </button>
                    </>
                  )}
              </div>
              {isAdmin && (
                <ul
                  aria-label={t('requests.requesterList')}
                  className="mt-4 border-t border-(--border-muted) pt-3 text-xs opacity-70"
                >
                  {group.map((request) => (
                    <li key={request.id}>
                      #{request.id}{' '}
                      {requesterName(
                        'requester_name' in request ? request.requester_name : null,
                        t('requests.unknownRequester'),
                      )}{' '}
                      · {request.status} · {request.profile_name ?? 'Default profile'}
                    </li>
                  ))}
                </ul>
              )}
            </article>
          );
        })
      )}
      {!loading && groups.size === 0 && (
        <p className="mt-6 rounded-xl border border-dashed border-(--border-muted) p-8 text-sm opacity-65">
          No {history ? 'historic' : 'active'} requests.
        </p>
      )}
      {interactiveSearch && (
        <InteractiveSearchModal
          requestProcess={interactiveSearch.requestProcess}
          title={interactiveSearch.title}
          author={interactiveSearch.author}
          profile={interactiveSearch.profile}
          onClose={() => setInteractiveSearch(null)}
          onShowToast={onShowToast}
          onChanged={load}
        />
      )}
    </section>
  );
};
