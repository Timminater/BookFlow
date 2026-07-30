import { useState, useRef, useMemo, type FormEvent, type KeyboardEvent } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { useLanguage } from '../contexts/LanguageContext';
import { useDismiss } from '../hooks/useDismiss';
import { useDependencyEffect, useMountEffect } from '../hooks/useMountEffect';
import { contextualBookPath } from '../pages/bookDetailContext';
import { updateSelfSettings } from '../services/api';
import { v1Api } from '../services/v1Api';
import type { ActingAsUserSelection } from '../types';
import type { V1SearchBook } from '../types/v1';
import { formatActingAsUserName } from '../utils/actingAsUser';
import { withBasePath } from '../utils/basePath';
import { isRecord } from '../utils/objectHelpers';
import { getStoredThemePreference } from '../utils/themePreference';
import { DropdownList } from './DropdownList';
import { IssueReportModal } from './IssueReportModal';
interface HeaderProps {
  debug?: boolean;
  isAdmin?: boolean;
  authRequired?: boolean;
  isAuthenticated?: boolean;
  issueReportingEnabled?: boolean;
  username?: string | null;
  displayName?: string | null;
  actingAsUser?: ActingAsUserSelection | null;
  onActingAsUserChange?: (user: ActingAsUserSelection | null) => void;
  adminUsers?: ActingAsUserSelection[];
  isAdminUsersLoading?: boolean;
  adminUsersError?: string | null;
  hasLoadedAdminUsers?: boolean;
  onLoadAdminUsers?: () => Promise<void> | void;
  onLogout?: () => void;
  onShowToast?: (
    message: string,
    type: 'success' | 'error' | 'info' | 'warning',
    persistent?: boolean,
  ) => string;
  onRemoveToast?: (id: string) => void;
}

const applyTheme = (preference: string): void => {
  let effective = preference;
  if (preference === 'auto') {
    effective = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  document.documentElement.setAttribute('data-theme', effective);
  document.documentElement.style.colorScheme = effective;
};

const EMPTY_ADMIN_USERS: ActingAsUserSelection[] = [];
export const Header = ({
  debug,
  isAdmin = false,
  authRequired = false,
  isAuthenticated = false,
  issueReportingEnabled = false,
  username,
  displayName,
  actingAsUser = null,
  onActingAsUserChange,
  adminUsers = EMPTY_ADMIN_USERS,
  isAdminUsersLoading = false,
  adminUsersError = null,
  hasLoadedAdminUsers = false,
  onLoadAdminUsers,
  onLogout,
  onShowToast,
  onRemoveToast,
}: HeaderProps) => {
  const navigate = useNavigate();
  const location = useLocation();
  const { language, setLanguage, t } = useLanguage();
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<V1SearchBook[]>([]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState(false);
  const [activeSearchIndex, setActiveSearchIndex] = useState(-1);
  const searchAbort = useRef<AbortController | null>(null);
  const searchRef = useRef<HTMLFormElement>(null);
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const [isClosing, setIsClosing] = useState(false);
  const [shouldAnimateIn, setShouldAnimateIn] = useState(false);
  const [isIssueReportOpen, setIsIssueReportOpen] = useState(false);
  let dropdownAnimationClass = '';
  if (isClosing) {
    dropdownAnimationClass = 'animate-fade-out-up';
  } else if (shouldAnimateIn) {
    dropdownAnimationClass = 'animate-fade-in-down';
  }
  const dropdownRef = useRef<HTMLDivElement>(null);

  const actingAsOptions = useMemo(
    () => [
      { value: '', label: t('header.myself') },
      ...adminUsers.map((user) => {
        const displayLabel = formatActingAsUserName(user);
        return {
          value: String(user.id),
          label: displayLabel,
          description: displayLabel !== user.username ? `@${user.username}` : undefined,
        };
      }),
    ],
    [adminUsers, t],
  );

  const selectedActingAsValue = actingAsUser ? String(actingAsUser.id) : '';
  const dropdownPanelWidthClass = 'w-48';

  useMountEffect(() => {
    const saved = getStoredThemePreference();
    applyTheme(saved);

    // Remove preload class and inline theme-init styles now that the
    // external CSS is loaded and React has mounted.
    requestAnimationFrame(() => {
      document.documentElement.classList.remove('preload');
      document.getElementById('theme-init')?.remove();
    });
  });

  useDependencyEffect(() => {
    searchAbort.current?.abort();
    const query = searchQuery.trim();
    setActiveSearchIndex(-1);
    setSearchError(false);
    if (query.length < 2) {
      setSearchResults([]);
      setSearchOpen(false);
      return undefined;
    }
    const controller = new AbortController();
    searchAbort.current = controller;
    setSearchLoading(true);
    setSearchOpen(true);
    const timer = window.setTimeout(() => {
      void v1Api
        .searchBooks(query, controller.signal)
        .then((response) => {
          if (!controller.signal.aborted) setSearchResults(response.items);
        })
        .catch(() => {
          if (!controller.signal.aborted) {
            setSearchResults([]);
            setSearchError(true);
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearchLoading(false);
        });
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [searchQuery]);

  useDependencyEffect(() => {
    setSearchOpen(false);
  }, [location.pathname, location.search]);

  useDependencyEffect(() => {
    if (!searchOpen) setActiveSearchIndex(-1);
  }, [searchOpen]);

  useMountEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = (e: MediaQueryListEvent) => {
      if (localStorage.getItem('bookflow-theme') === 'auto') {
        const effective = e.matches ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', effective);
        document.documentElement.style.colorScheme = effective;
      }
    };
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  });

  // Helper function to close dropdown with animation
  const closeDropdown = () => {
    setIsClosing(true);
    setTimeout(() => {
      setIsDropdownOpen(false);
      setIsClosing(false);
    }, 150); // Match the animation duration
  };

  useDismiss(isDropdownOpen && !isClosing, [dropdownRef], closeDropdown);
  useDismiss(searchOpen, [searchRef], () => setSearchOpen(false));

  const handleLogout = () => {
    closeDropdown();
    onLogout?.();
  };

  const toggleDropdown = () => {
    if (isDropdownOpen) {
      closeDropdown();
    } else {
      if (isAdmin && !hasLoadedAdminUsers && !isAdminUsersLoading) {
        void onLoadAdminUsers?.();
      }
      setShouldAnimateIn(true);
      setIsDropdownOpen(true);
      // Reset animation flag after animation completes
      setTimeout(() => setShouldAnimateIn(false), 200);
    }
  };

  const handleActingAsChange = (nextValue: string[] | string) => {
    if (Array.isArray(nextValue)) {
      return;
    }

    if (nextValue === '') {
      onActingAsUserChange?.(null);
      return;
    }

    const selectedUser = adminUsers.find((user) => String(user.id) === nextValue);
    if (!selectedUser) {
      return;
    }

    onActingAsUserChange?.(selectedUser);
  };

  const handleDebugDownload = async () => {
    closeDropdown();
    // Show persistent toast while gathering logs
    const loadingToastId = onShowToast?.(t('header.debugGathering'), 'info', true);
    try {
      const response = await fetch(withBasePath('/api/debug'), {
        method: 'GET',
        credentials: 'include',
      });

      // Remove the loading toast
      if (loadingToastId) onRemoveToast?.(loadingToastId);

      if (!response.ok) {
        const errorData: unknown = await response.json().catch(() => null);
        const errorMessage =
          isRecord(errorData) && typeof errorData.error === 'string'
            ? errorData.error
            : response.statusText;
        onShowToast?.(t('header.debugFailedDetail', { error: errorMessage }), 'error');
        return;
      }

      // Get the filename from Content-Disposition header or use default
      const contentDisposition = response.headers.get('Content-Disposition');
      let filename = 'debug.zip';
      if (contentDisposition) {
        const filenameMatch = contentDisposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
        if (filenameMatch && filenameMatch[1]) {
          filename = filenameMatch[1].replace(/['"]/g, '');
        }
      }

      // Create blob and trigger download
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      a.remove();

      onShowToast?.(t('header.debugSuccess'), 'success');
    } catch (error) {
      // Remove the loading toast on error too
      if (loadingToastId) onRemoveToast?.(loadingToastId);
      console.error('Debug download error:', error);
      onShowToast?.(t('header.debugFailed'), 'error');
    }
  };

  const handleCatalogSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const query = searchQuery.trim();
    void navigate(query ? `/catalog?q=${encodeURIComponent(query)}` : '/catalog');
    setSearchOpen(false);
  };
  const openSearchResult = (book: V1SearchBook) => {
    setSearchOpen(false);
    let context: 'library' | 'trending' | 'catalog' = 'catalog';
    if (book.in_library) context = 'library';
    else if (book.is_trending) context = 'trending';
    void navigate(contextualBookPath(context, book.id));
  };
  const searchStatusLabel = (book: V1SearchBook) => {
    if (book.in_library) return t('search.inLibrary');
    if (book.catalog_visible && book.has_imported_file) return t('search.catalog');
    if (book.is_trending) return t('search.trending');
    return t('search.metadata');
  };
  let activeSearchOptionId: string | undefined;
  if (searchOpen && activeSearchIndex >= 0) {
    const activeBook = searchResults[activeSearchIndex];
    if (activeBook) activeSearchOptionId = `global-book-search-result-${activeBook.id}`;
    else if (activeSearchIndex === searchResults.length) {
      activeSearchOptionId = 'global-book-search-external';
    }
  }
  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (!searchOpen) return;
    const lastIndex = searchResults.length;
    if (event.key === 'Escape') {
      setSearchOpen(false);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      setActiveSearchIndex((current) => {
        if (current < 0) return direction === 1 ? 0 : lastIndex;
        return (current + direction + lastIndex + 1) % (lastIndex + 1);
      });
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      setActiveSearchIndex(event.key === 'Home' ? 0 : lastIndex);
    } else if (event.key === 'Enter' && activeSearchIndex >= 0) {
      event.preventDefault();
      const selected = searchResults[activeSearchIndex];
      if (selected) openSearchResult(selected);
      else void navigate(`/catalog?q=${encodeURIComponent(searchQuery.trim())}&external=1`);
      setSearchOpen(false);
    }
  };
  const handleLanguageChange = async (nextLanguage: 'en' | 'nl') => {
    setLanguage(nextLanguage);
    try {
      await updateSelfSettings({ preferred_language: nextLanguage });
    } catch {
      onShowToast?.(t('header.languageSaveFailed'), 'error');
    }
  };

  // Icon buttons - reused for both states
  const iconButtonsNode = (
    <div className="flex items-center gap-2">
      {/* User Menu Dropdown */}
      <div className="relative" ref={dropdownRef}>
        <button
          type="button"
          onClick={toggleDropdown}
          className={`hover-action relative rounded-full p-2 transition-colors ${
            isDropdownOpen ? 'bg-(--hover-action)' : ''
          }`}
          aria-label={t('header.userMenu')}
          aria-expanded={isDropdownOpen}
          aria-haspopup="true"
        >
          <svg
            className="h-5 w-5"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth="1.5"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.5 20.12a7.5 7.5 0 0115 0 17.93 17.93 0 01-7.5 1.63 17.93 17.93 0 01-7.5-1.63z"
            />
          </svg>
          {actingAsUser && (
            <span
              className="absolute top-1 right-1 h-2 w-2 rounded-full border border-(--bg) bg-sky-500"
              title={t('header.downloadingAs', { user: formatActingAsUserName(actingAsUser) })}
            />
          )}
        </button>

        {/* Dropdown Menu */}
        {(isDropdownOpen || isClosing) && (
          <div
            className={`absolute right-0 mt-2 ${dropdownPanelWidthClass} z-50 rounded-lg border shadow-lg ${
              dropdownAnimationClass
            }`}
            style={{
              background: 'var(--bg)',
              borderColor: 'var(--border-muted)',
            }}
          >
            <div className="py-1">
              <div className="border-b border-(--border-muted) px-4 py-3">
                <div className="mb-2 text-[10px] font-semibold tracking-widest uppercase opacity-50">
                  {t('header.language')}
                </div>
                <div className="grid grid-cols-2 gap-1 rounded-lg bg-(--hover-surface) p-1">
                  {(['en', 'nl'] as const).map((option) => (
                    <button
                      type="button"
                      key={option}
                      className={`rounded-md px-2 py-1.5 text-xs font-medium transition-colors ${
                        language === option ? 'bg-(--bg-soft) shadow-sm' : 'opacity-60'
                      }`}
                      onClick={() => void handleLanguageChange(option)}
                    >
                      {t(`language.${option}`)}
                    </button>
                  ))}
                </div>
              </div>
              {issueReportingEnabled && isAuthenticated && (
                <button
                  type="button"
                  onClick={() => {
                    closeDropdown();
                    setIsIssueReportOpen(true);
                  }}
                  className="hover-surface flex w-full items-center gap-3 px-4 py-2 text-left text-slate-700 transition-colors dark:text-slate-200"
                >
                  <svg
                    className="h-5 w-5"
                    xmlns="http://www.w3.org/2000/svg"
                    fill="none"
                    viewBox="0 0 24 24"
                    strokeWidth="1.5"
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M3 3v1.5M3 21v-6m0 0 2.77-.693a9 9 0 0 1 6.208.682l.108.054a9 9 0 0 0 6.086.71l3.114-.732a48.524 48.524 0 0 1-.005-10.499l-3.11.732a9 9 0 0 1-6.085-.711l-.108-.054a9 9 0 0 0-6.208-.682L3 4.5M3 15V4.5"
                    />
                  </svg>
                  <span>{t('header.reportBug')}</span>
                </button>
              )}

              {/* Debug Buttons */}
              {debug && (
                <>
                  <button
                    type="button"
                    className="hover-surface flex w-full items-center gap-3 px-4 py-2 text-left text-orange-600 transition-colors dark:text-orange-400"
                    onClick={() => void handleDebugDownload()}
                  >
                    <svg
                      className="h-5 w-5"
                      xmlns="http://www.w3.org/2000/svg"
                      fill="none"
                      viewBox="0 0 24 24"
                      strokeWidth="1.5"
                      stroke="currentColor"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M12 12.75c1.148 0 2.278.08 3.383.237 1.037.146 1.866.966 1.866 2.013 0 3.728-2.35 6.75-5.25 6.75S6.75 18.728 6.75 15c0-1.046.83-1.867 1.866-2.013A24.204 24.204 0 0112 12.75zm0 0c2.883 0 5.647.508 8.207 1.44a23.91 23.91 0 01-1.152 6.06M12 12.75c-2.883 0-5.647.508-8.208 1.44.125 2.104.52 4.136 1.153 6.06M12 12.75a2.25 2.25 0 002.248-2.354M12 12.75a2.25 2.25 0 01-2.248-2.354M12 8.25c.995 0 1.971-.08 2.922-.236.403-.066.74-.358.795-.762a3.778 3.778 0 00-.399-2.25M12 8.25c-.995 0-1.97-.08-2.922-.236-.402-.066-.74-.358-.795-.762a3.734 3.734 0 01.4-2.253M12 8.25a2.25 2.25 0 00-2.248 2.146M12 8.25a2.25 2.25 0 012.248 2.146M8.683 5a6.032 6.032 0 01-1.155-1.002c.07-.63.27-1.222.574-1.747m.581 2.749A3.75 3.75 0 0115.318 5m0 0c.427-.283.815-.62 1.155-.999a4.471 4.471 0 00-.575-1.752M4.921 6a24.048 24.048 0 00-.392 3.314c1.668.546 3.416.914 5.223 1.082M19.08 6c.205 1.08.337 2.187.392 3.314a23.882 23.882 0 01-5.223 1.082"
                      />
                    </svg>
                    <span>Debug</span>
                  </button>
                  <form action={withBasePath('/api/restart')} method="post" className="w-full">
                    <button
                      className="hover-surface flex w-full items-center gap-3 px-4 py-2 text-left text-orange-600 transition-colors dark:text-orange-400"
                      type="submit"
                    >
                      <svg
                        className="h-5 w-5"
                        xmlns="http://www.w3.org/2000/svg"
                        fill="none"
                        viewBox="0 0 24 24"
                        strokeWidth="1.5"
                        stroke="currentColor"
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"
                        />
                      </svg>
                      <span>{t('header.restart')}</span>
                    </button>
                  </form>
                </>
              )}

              {/* User Footer */}
              {authRequired && isAuthenticated && username && (
                <div className="border-t" style={{ borderColor: 'var(--border-muted)' }}>
                  <div className="flex items-center gap-2.5 px-4 py-3">
                    <span
                      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold uppercase"
                      style={{ backgroundColor: 'var(--hover-surface)', color: 'var(--text)' }}
                    >
                      {(displayName || username).slice(0, 2)}
                    </span>
                    <div className="min-w-0 flex-1 truncate text-sm font-medium">
                      {displayName || username}
                    </div>
                    {onLogout && (
                      <button
                        type="button"
                        onClick={handleLogout}
                        className="hover-action shrink-0 rounded-full p-2 text-red-600 transition-colors dark:text-red-400"
                        title={t('header.signOut')}
                        aria-label={t('header.signOut')}
                      >
                        <svg
                          className="h-5 w-5"
                          xmlns="http://www.w3.org/2000/svg"
                          fill="none"
                          viewBox="0 0 24 24"
                          strokeWidth="1.5"
                          stroke="currentColor"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            d="M15.75 9V5.25A2.25 2.25 0 0013.5 3h-6a2.25 2.25 0 00-2.25 2.25v13.5A2.25 2.25 0 007.5 21h6a2.25 2.25 0 002.25-2.25V15m3 0l3-3m0 0l-3-3m3 3H9"
                          />
                        </svg>
                      </button>
                    )}
                  </div>
                </div>
              )}

              {isAdmin && onActingAsUserChange && (
                <div
                  className="space-y-2 border-t px-4 py-3"
                  style={{ borderColor: 'var(--border-muted)' }}
                >
                  <div className="text-xs font-medium tracking-wide uppercase opacity-70">
                    {t('header.downloadAs')}
                  </div>
                  <div className={isAdminUsersLoading ? 'pointer-events-none opacity-60' : ''}>
                    <DropdownList
                      options={actingAsOptions}
                      value={selectedActingAsValue}
                      onChange={handleActingAsChange}
                      placeholder={t('header.myself')}
                      widthClassName="w-full"
                      buttonClassName="rounded-lg text-sm"
                    />
                  </div>
                  {isAdminUsersLoading && (
                    <div className="text-xs opacity-70">{t('header.loadingUsers')}</div>
                  )}
                  {adminUsersError && (
                    <div className="flex items-center justify-between gap-3">
                      <div className="text-xs text-red-600 dark:text-red-400">
                        {adminUsersError}
                      </div>
                      <button
                        type="button"
                        onClick={() => void onLoadAdminUsers?.()}
                        className="text-xs font-medium text-sky-600 hover:text-sky-700 dark:text-sky-400 dark:hover:text-sky-300"
                      >
                        {t('common.retry')}
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );

  return (
    <>
      <header
        className="bookflow-header sticky top-0 z-40 w-full border-b border-(--border-muted) bg-(--bg) backdrop-blur-xl lg:pl-64"
        style={{ paddingTop: 'env(safe-area-inset-top)' }}
      >
        <div className="mx-auto max-w-[96rem] px-4 py-3 sm:px-6 lg:px-8">
          <div className="relative flex min-h-[48px] items-center gap-3 pl-11 lg:pl-0 xl:justify-center">
            <form
              ref={searchRef}
              className="relative min-w-0 flex-1 xl:w-full xl:max-w-2xl xl:flex-none"
              onSubmit={handleCatalogSearch}
            >
              <span
                aria-hidden="true"
                className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-sm opacity-45"
              >
                ⌕
              </span>
              <input
                type="search"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                onFocus={() => searchQuery.trim().length >= 2 && setSearchOpen(true)}
                onKeyDown={handleSearchKeyDown}
                placeholder={t('header.search')}
                aria-label={t('header.searchLabel')}
                role="combobox"
                aria-autocomplete="list"
                aria-expanded={searchOpen}
                aria-controls="global-book-search-results"
                aria-activedescendant={activeSearchOptionId}
                className="w-full rounded-xl border border-(--border-muted) bg-(--bg-soft) py-2.5 pr-4 pl-9 text-sm shadow-sm transition outline-none focus:border-violet-500/60 focus:ring-2 focus:ring-violet-500/15"
              />
              {searchOpen && (
                <div
                  id="global-book-search-results"
                  role="listbox"
                  className="absolute z-50 mt-2 max-h-[60vh] w-full overflow-y-auto rounded-xl border border-(--border-muted) bg-(--bg) p-1 shadow-xl"
                >
                  {searchLoading && (
                    <p role="status" aria-live="polite" className="px-3 py-2 text-sm">
                      {t('search.loading')}
                    </p>
                  )}
                  {!searchLoading && searchError && (
                    <p role="alert" className="px-3 py-2 text-sm">
                      {t('search.error')}
                    </p>
                  )}
                  {!searchLoading && !searchError && searchResults.length === 0 && (
                    <p role="status" className="px-3 py-2 text-sm">
                      {t('search.empty')}
                    </p>
                  )}
                  {searchResults.map((book, index) => (
                    <button
                      type="button"
                      id={`global-book-search-result-${book.id}`}
                      role="option"
                      aria-selected={activeSearchIndex === index}
                      key={book.id}
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => openSearchResult(book)}
                      className="hover-surface flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left"
                    >
                      {book.cover_url && (
                        <img
                          src={book.cover_url}
                          alt=""
                          className="h-12 w-8 shrink-0 rounded object-cover"
                        />
                      )}
                      <span className="min-w-0 flex-1">
                        <strong className="block truncate">{book.title}</strong>
                        <small className="block truncate">{book.author}</small>
                      </span>
                      <small>{searchStatusLabel(book)}</small>
                    </button>
                  ))}
                  <button
                    type="button"
                    id="global-book-search-external"
                    role="option"
                    aria-selected={activeSearchIndex === searchResults.length}
                    onClick={() => {
                      setSearchOpen(false);
                      void navigate(
                        `/catalog?q=${encodeURIComponent(searchQuery.trim())}&external=1`,
                      );
                    }}
                    className="hover-surface mt-1 w-full rounded-lg px-3 py-2 text-left text-sm font-medium"
                  >
                    {t('search.external', { query: searchQuery.trim() })}
                  </button>
                </div>
              )}
            </form>
            <div className="xl:absolute xl:right-0">{iconButtonsNode}</div>
          </div>
        </div>
      </header>
      <IssueReportModal
        isOpen={isIssueReportOpen}
        onClose={() => setIsIssueReportOpen(false)}
        onShowToast={onShowToast}
      />
    </>
  );
};
