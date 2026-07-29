import { useCallback, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { useLanguage } from '../../contexts/LanguageContext';
import { useDependencyEffect } from '../../hooks/useMountEffect';

interface DashboardShelfProps {
  id: string;
  title: string;
  href?: string;
  hrefLabel?: string;
  meta?: ReactNode;
  children: ReactNode;
  itemCount: number;
  loading?: boolean;
  error?: string;
  emptyMessage: string;
  onRetry: () => void;
  skeletonVariant?: 'poster' | 'wide';
}

const Skeletons = ({ variant }: { variant: 'poster' | 'wide' }) => (
  <div className="flex gap-3 overflow-hidden pb-2 sm:gap-4">
    {Array.from({ length: variant === 'wide' ? 5 : 9 }, (_, index) => (
      <div
        key={index}
        className={`shrink-0 animate-pulse rounded-xl border border-(--border-muted) bg-(--bg-soft) ${
          variant === 'wide'
            ? 'min-h-36 w-full max-w-[19rem] sm:max-w-[23rem]'
            : 'aspect-[2/3] w-28 sm:w-[7.5rem]'
        }`}
      />
    ))}
  </div>
);

export const DashboardShelf = ({
  id,
  title,
  href,
  hrefLabel,
  meta,
  children,
  itemCount,
  loading = false,
  error,
  emptyMessage,
  onRetry,
  skeletonVariant = 'poster',
}: DashboardShelfProps) => {
  const { t } = useLanguage();
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [canScrollBack, setCanScrollBack] = useState(false);
  const [canScrollForward, setCanScrollForward] = useState(false);

  const updateScrollButtons = useCallback(() => {
    const element = scrollerRef.current;
    if (!element) return;
    setCanScrollBack(element.scrollLeft > 4);
    setCanScrollForward(element.scrollLeft + element.clientWidth < element.scrollWidth - 4);
  }, []);

  useDependencyEffect(() => {
    updateScrollButtons();
    const element = scrollerRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(updateScrollButtons);
    observer.observe(element);
    return () => observer.disconnect();
  }, [itemCount, loading, updateScrollButtons]);

  const scroll = (direction: -1 | 1) => {
    const element = scrollerRef.current;
    if (!element) return;
    element.scrollBy({ left: direction * element.clientWidth * 0.82, behavior: 'smooth' });
  };

  let shelfContent: ReactNode;
  if (loading) {
    shelfContent = <Skeletons variant={skeletonVariant} />;
  } else if (error) {
    shelfContent = (
      <div className="rounded-xl border border-red-500/25 bg-red-500/5 px-4 py-5 text-sm text-red-400">
        {error}{' '}
        <button type="button" className="font-medium underline" onClick={onRetry}>
          {t('common.retry')}
        </button>
      </div>
    );
  } else if (itemCount === 0) {
    shelfContent = (
      <div className="rounded-xl border border-(--border-muted) bg-(--bg-soft) px-4 py-6 text-sm opacity-60">
        {emptyMessage}
      </div>
    );
  } else {
    shelfContent = (
      <div className="relative">
        <div
          ref={scrollerRef}
          onScroll={updateScrollButtons}
          className="scrollbar-hide flex snap-x snap-mandatory gap-3 overflow-x-auto overscroll-x-contain pb-3 sm:gap-4"
        >
          {children}
        </div>
      </div>
    );
  }

  return (
    <section className="min-w-0" aria-labelledby={id}>
      <div className="mb-3 flex items-center gap-3">
        <h2 id={id} className="text-xl font-semibold tracking-tight sm:text-2xl">
          {title}
        </h2>
        {href && (
          <Link
            to={href}
            aria-label={hrefLabel ?? title}
            title={hrefLabel ?? title}
            className="flex h-6 w-6 items-center justify-center rounded-full border border-(--border-muted) text-sm opacity-70 transition hover:border-violet-500 hover:text-violet-400 hover:opacity-100"
          >
            →
          </Link>
        )}
        {meta}
        <div className="ml-auto hidden items-center gap-1 md:flex">
          <button
            type="button"
            aria-label={`${title}: ${t('dashboard.scrollBack')}`}
            disabled={!canScrollBack}
            onClick={() => scroll(-1)}
            className="flex h-8 w-8 items-center justify-center rounded-full text-xl opacity-60 transition hover:bg-(--hover-surface) hover:opacity-100 disabled:opacity-15"
          >
            ‹
          </button>
          <button
            type="button"
            aria-label={`${title}: ${t('dashboard.scrollForward')}`}
            disabled={!canScrollForward}
            onClick={() => scroll(1)}
            className="flex h-8 w-8 items-center justify-center rounded-full text-xl opacity-60 transition hover:bg-(--hover-surface) hover:opacity-100 disabled:opacity-15"
          >
            ›
          </button>
        </div>
      </div>

      {shelfContent}
    </section>
  );
};
