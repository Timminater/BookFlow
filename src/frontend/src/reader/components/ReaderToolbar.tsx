import { useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';

import { useLanguage } from '../../contexts/LanguageContext';
import { useDismiss } from '../../hooks/useDismiss';
import { formatReaderProgress } from '../readerProgress';
import type { ReaderLocator, ReaderPreferences } from '../types';

interface ReaderToolbarProps {
  title: string;
  controlsVisible: boolean;
  position: ReaderLocator;
  preferences: ReaderPreferences;
  onBack: () => void;
  onToggleToc: () => void;
  onToggleSettings: () => void;
  onToggleFullscreen: () => void;
  onDownload: () => Promise<void>;
  onShare: () => Promise<void>;
}

const desktopButtonClass =
  'rounded-md px-2 py-1.5 text-sm hover:bg-black/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:hover:bg-white/10';
const mobileIconButtonClass =
  'grid h-11 w-11 shrink-0 place-items-center rounded-md hover:bg-black/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:hover:bg-white/10';
const menuItemClass =
  'block min-h-11 w-full px-4 py-2 text-left text-sm hover:bg-black/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sky-500 dark:hover:bg-white/10';

const ToolbarIcon = ({ kind }: { kind: 'back' | 'more' }) => (
  <svg
    aria-hidden="true"
    className="h-5 w-5"
    fill="none"
    viewBox="0 0 24 24"
    stroke="currentColor"
    strokeWidth="1.8"
  >
    {kind === 'back' ? (
      <path strokeLinecap="round" strokeLinejoin="round" d="m15 18-6-6 6-6" />
    ) : (
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M5.25 12h.008v.008H5.25V12Zm6.75 0h.008v.008H12V12Zm6.75 0h.008v.008h-.008V12Z"
      />
    )}
  </svg>
);

const handleMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  const items = Array.from(
    event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'),
  );
  if (items.length === 0) return;
  event.preventDefault();
  const currentIndex = items.findIndex((item) => item === document.activeElement);
  if (event.key === 'Home') {
    items[0]?.focus();
  } else if (event.key === 'End') {
    items.at(-1)?.focus();
  } else {
    const direction = event.key === 'ArrowDown' ? 1 : -1;
    const nextIndex = (currentIndex + direction + items.length) % items.length;
    items[nextIndex]?.focus();
  }
};

export const ReaderToolbar = ({
  title,
  controlsVisible,
  position,
  preferences,
  onBack,
  onToggleToc,
  onToggleSettings,
  onToggleFullscreen,
  onDownload,
  onShare,
}: ReaderToolbarProps) => {
  const { t } = useLanguage();
  const [busyAction, setBusyAction] = useState<'download' | 'share' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionsOpen, setActionsOpen] = useState(false);
  const actionsRef = useRef<HTMLDivElement>(null);
  const actionsButtonRef = useRef<HTMLButtonElement>(null);

  const closeActions = () => {
    setActionsOpen(false);
    actionsButtonRef.current?.focus();
  };

  useDismiss(actionsOpen, [actionsRef], closeActions);

  const runAction = (kind: 'download' | 'share', action: () => Promise<void>) => {
    setBusyAction(kind);
    setActionError(null);
    void action()
      .catch((caught: unknown) => {
        if (caught instanceof DOMException && caught.name === 'AbortError') return;
        setActionError(caught instanceof Error ? caught.message : t('webReader.actionFailed'));
      })
      .finally(() => setBusyAction(null));
  };

  const menuAction = (action: () => void) => {
    setActionsOpen(false);
    action();
  };

  return (
    <>
      <header
        className={`absolute inset-x-0 top-0 z-20 border-b border-black/10 transition-transform dark:border-white/10 ${
          controlsVisible ? 'translate-y-0' : '-translate-y-full'
        } ${preferences.theme === 'dark' ? 'bg-slate-950 text-slate-100' : 'bg-white/95 text-slate-900'}`}
        style={{ paddingTop: 'env(safe-area-inset-top)' }}
      >
        <div className="flex min-h-12 min-w-0 items-center gap-1 px-2 sm:gap-2 sm:px-3">
          <button
            type="button"
            className={`${mobileIconButtonClass} sm:hidden`}
            onClick={onBack}
            aria-label={t('webReader.close')}
            title={t('webReader.close')}
          >
            <ToolbarIcon kind="back" />
          </button>
          <button
            type="button"
            className={`${desktopButtonClass} hidden sm:inline-flex`}
            onClick={onBack}
          >
            {t('webReader.close')}
          </button>
          <p className="min-w-0 flex-1 truncate text-sm font-medium" title={title}>
            {title}
          </p>
          <span className="shrink-0 text-xs opacity-65" aria-label={t('webReader.readingProgress')}>
            {formatReaderProgress(position)}
          </span>
          <div className="hidden items-center gap-2 sm:flex">
            <button
              type="button"
              className={desktopButtonClass}
              disabled={busyAction !== null}
              onClick={() => runAction('download', onDownload)}
            >
              {busyAction === 'download' ? t('webReader.downloading') : t('webReader.download')}
            </button>
            <button
              type="button"
              className={desktopButtonClass}
              disabled={busyAction !== null}
              onClick={() => runAction('share', onShare)}
            >
              {busyAction === 'share' ? t('webReader.opening') : t('webReader.openOtherApp')}
            </button>
            <button type="button" className={desktopButtonClass} onClick={onToggleToc}>
              {t('webReader.contents')}
            </button>
            <button type="button" className={desktopButtonClass} onClick={onToggleSettings}>
              {t('webReader.display')}
            </button>
            <button type="button" className={desktopButtonClass} onClick={onToggleFullscreen}>
              {t('webReader.fullscreen')}
            </button>
          </div>
          <div className="relative sm:hidden" ref={actionsRef}>
            <button
              ref={actionsButtonRef}
              type="button"
              className={mobileIconButtonClass}
              aria-label={t('webReader.moreActions')}
              title={t('webReader.moreActions')}
              aria-expanded={actionsOpen}
              aria-haspopup="menu"
              onClick={() => setActionsOpen((open) => !open)}
            >
              <ToolbarIcon kind="more" />
            </button>
            {actionsOpen && (
              <div
                role="menu"
                tabIndex={-1}
                aria-label={t('webReader.readerActions')}
                className="absolute top-full right-0 z-30 mt-1 w-56 overflow-hidden rounded-lg border border-black/10 bg-white py-1 text-slate-900 shadow-xl dark:border-white/10 dark:bg-slate-900 dark:text-slate-100"
                onKeyDown={handleMenuKeyDown}
              >
                <button
                  ref={(element) => element?.focus()}
                  role="menuitem"
                  type="button"
                  className={menuItemClass}
                  disabled={busyAction !== null}
                  onClick={() => menuAction(() => runAction('download', onDownload))}
                >
                  {t('webReader.download')}
                </button>
                <button
                  role="menuitem"
                  type="button"
                  className={menuItemClass}
                  disabled={busyAction !== null}
                  onClick={() => menuAction(() => runAction('share', onShare))}
                >
                  {t('webReader.openOtherApp')}
                </button>
                <button
                  role="menuitem"
                  type="button"
                  className={menuItemClass}
                  onClick={() => menuAction(onToggleToc)}
                >
                  {t('webReader.contents')}
                </button>
                <button
                  role="menuitem"
                  type="button"
                  className={menuItemClass}
                  onClick={() => menuAction(onToggleSettings)}
                >
                  {t('webReader.display')}
                </button>
                <button
                  role="menuitem"
                  type="button"
                  className={menuItemClass}
                  onClick={() => menuAction(onToggleFullscreen)}
                >
                  {t('webReader.fullscreen')}
                </button>
              </div>
            )}
          </div>
        </div>
      </header>
      {actionError && (
        <p
          role="alert"
          className="absolute right-3 z-30 max-w-sm rounded-md bg-red-700 px-3 py-2 text-sm text-white shadow-lg"
          style={{ top: 'calc(3.5rem + env(safe-area-inset-top))' }}
        >
          {actionError}
        </p>
      )}
    </>
  );
};
