import { useCallback, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';

import { useDependencyEffect } from '../../hooks/useMountEffect';
import type { ReaderAdapterFactory } from '../adapters/unavailableAdapter';
import { initializeReaderAdapter } from '../readerInitialization';
import { keyboardNavigationAction, tapZoneAction } from '../readerNavigation';
import { loadReaderPreferences, saveReaderPreferences } from '../readerPreferences';
import type {
  ReaderAdapter,
  ReaderFile,
  ReaderLocator,
  ReaderPreferences,
  ReaderTocItem,
} from '../types';
import { ReaderSettingsPanel } from './ReaderSettingsPanel';
import { ReaderTocPanel } from './ReaderTocPanel';
import { ReaderToolbar } from './ReaderToolbar';

interface BrowserReaderProps {
  file: ReaderFile;
  initialLocator?: ReaderLocator | null;
  adapterFactory: ReaderAdapterFactory;
  onBack: () => void;
  onProgressChange: (locator: ReaderLocator) => void;
  onDownload: () => Promise<void>;
  onShare: () => Promise<void>;
}

const targetIsInteractive = (target: EventTarget | null): boolean => {
  if (!(target instanceof Element)) return false;
  return Boolean(target.closest('a, button, input, select, textarea, [contenteditable="true"]'));
};

const themeClass = (theme: ReaderPreferences['theme']): string => {
  if (theme === 'dark') return 'bg-slate-950 text-slate-100';
  if (theme === 'sepia') return 'bg-[#f4ecd8] text-[#433422]';
  return 'bg-white text-slate-900';
};

export const BrowserReader = ({
  file,
  initialLocator,
  adapterFactory,
  onBack,
  onProgressChange,
  onDownload,
  onShare,
}: BrowserReaderProps) => {
  const viewportRef = useRef<HTMLDivElement>(null);
  const adapterRef = useRef<ReaderAdapter | null>(null);
  const preferencesRef = useRef<ReaderPreferences>(loadReaderPreferences());
  const pointerRef = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const [preferences, setPreferences] = useState<ReaderPreferences>(loadReaderPreferences);
  const [progress, setProgress] = useState(0);
  const [toc, setToc] = useState<ReaderTocItem[]>([]);
  const [controlsVisible, setControlsVisible] = useState(true);
  const [tocOpen, setTocOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  preferencesRef.current = preferences;

  const handleLocator = useCallback(
    (locator: ReaderLocator) => {
      const normalized: ReaderLocator = {
        ...locator,
        progression: Math.max(0, Math.min(1, locator.progression)),
      };
      setProgress(normalized.progression);
      onProgressChange(normalized);
    },
    [onProgressChange],
  );

  const navigate = useCallback((direction: 'previous' | 'next') => {
    const adapter = adapterRef.current;
    if (!adapter) return;
    if (direction === 'previous') {
      void adapter.previous();
    } else {
      void adapter.next();
    }
  }, []);

  useDependencyEffect(() => {
    const container = viewportRef.current;
    if (!container) return undefined;
    let disposed = false;
    let unsubscribe: (() => void) | null = null;
    setLoading(true);
    setError(null);
    void adapterFactory(file)
      .then(async (adapter) => {
        if (disposed) {
          adapter.close();
          return;
        }
        adapterRef.current = adapter;
        unsubscribe = await initializeReaderAdapter({
          adapter,
          container,
          file,
          initialLocator,
          preferences: preferencesRef.current,
          isDisposed: () => disposed,
          onLocator: handleLocator,
        });
        if (disposed || !unsubscribe) return;
        setToc(adapter.getToc());
        setLoading(false);
      })
      .catch((caught: unknown) => {
        if (!disposed) {
          setError(caught instanceof Error ? caught.message : 'The book could not be opened.');
          setLoading(false);
        }
      });
    return () => {
      disposed = true;
      unsubscribe?.();
      adapterRef.current?.close();
      adapterRef.current = null;
    };
  }, [adapterFactory, file, handleLocator, initialLocator]);

  useDependencyEffect(() => {
    saveReaderPreferences(preferences);
    const adapter = adapterRef.current;
    if (adapter) void adapter.setPreferences(preferences);
  }, [preferences]);

  useDependencyEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (tocOpen || settingsOpen) return;
      const action = keyboardNavigationAction(event);
      if (!action) return;
      event.preventDefault();
      navigate(action);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [navigate, settingsOpen, tocOpen]);

  const applyPreferences = (next: ReaderPreferences) => setPreferences(next);

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    pointerRef.current = { x: event.clientX, y: event.clientY, moved: false };
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = pointerRef.current;
    if (!start) return;
    if (Math.hypot(event.clientX - start.x, event.clientY - start.y) > 8) start.moved = true;
  };

  const handlePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = pointerRef.current;
    pointerRef.current = null;
    if (!start || start.moved || window.getSelection()?.toString()) return;
    const action = tapZoneAction(
      event.clientX,
      window.innerWidth,
      targetIsInteractive(event.target),
    );
    if (action === 'toggle-controls') setControlsVisible((visible) => !visible);
    if (action === 'previous' || action === 'next') navigate(action);
  };

  const toggleFullscreen = () => {
    const element = viewportRef.current?.parentElement;
    if (!element) return;
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      void element.requestFullscreen?.();
    }
  };

  return (
    <section className={`fixed inset-0 z-50 overflow-hidden ${themeClass(preferences.theme)}`}>
      <ReaderToolbar
        title={file.title}
        controlsVisible={controlsVisible}
        progress={progress}
        preferences={preferences}
        onBack={onBack}
        onToggleToc={() => {
          setTocOpen((open) => !open);
          setSettingsOpen(false);
        }}
        onToggleSettings={() => {
          setSettingsOpen((open) => !open);
          setTocOpen(false);
        }}
        onToggleFullscreen={toggleFullscreen}
        onDownload={onDownload}
        onShare={onShare}
      />
      {tocOpen && (
        <ReaderTocPanel
          items={toc}
          onClose={() => setTocOpen(false)}
          onSelect={(item) => {
            if (item.href) void adapterRef.current?.goTo(item.href);
            setTocOpen(false);
          }}
        />
      )}
      {settingsOpen && (
        <ReaderSettingsPanel
          preferences={preferences}
          onChange={applyPreferences}
          onClose={() => setSettingsOpen(false)}
        />
      )}
      <div
        ref={viewportRef}
        className="h-full w-full pt-[calc(3rem+env(safe-area-inset-top))]"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        aria-busy={loading}
      />
      {loading && !error && (
        <p className="pointer-events-none absolute inset-0 grid place-items-center text-sm opacity-70">
          Opening book…
        </p>
      )}
      {error && (
        <div className="absolute inset-0 grid place-items-center p-6">
          <div className="max-w-md rounded-lg border border-red-400/35 bg-red-500/10 p-5 text-center">
            <p className="font-medium">Unable to open this book</p>
            <p className="mt-2 text-sm opacity-80">{error}</p>
            <button
              type="button"
              className="mt-4 rounded bg-sky-700 px-3 py-2 text-sm text-white"
              onClick={onBack}
            >
              Back to book
            </button>
          </div>
        </div>
      )}
    </section>
  );
};
