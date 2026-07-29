import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { V1UserRequest } from '../types/v1';

vi.mock('react-router-dom', () => ({
  Link: ({
    children,
    to,
    ...props
  }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) => (
    <a {...props} href={to}>
      {children}
    </a>
  ),
}));

vi.mock('../contexts/LanguageContext', () => ({
  useLanguage: () => ({
    language: 'nl',
    t: (key: string) =>
      ({
        'dashboard.requestBook': 'Boek',
        'dashboard.requestStatus.available': 'Beschikbaar',
        'dashboard.requestStatus.processing': 'In behandeling',
        'dashboard.requestStatus.rejected': 'Afgewezen',
        'dashboard.requestStatus.cancelled': 'Geannuleerd',
        'dashboard.requestStatus.failed': 'Mislukt',
        'dashboard.requestStatus.unknown': 'Onbekend',
        'common.unknownAuthor': 'Onbekende auteur',
      })[key] ?? key,
  }),
}));

import { DashboardShelf } from '../components/dashboard/DashboardShelf';
import { RequestCard } from '../components/dashboard/RequestCard';

const request = (overrides: Partial<V1UserRequest> = {}): V1UserRequest => ({
  id: 1,
  book_id: 42,
  title: 'Een titel',
  author: 'Een auteur',
  cover_url: '/covers/42.jpg',
  content_type: 'ebook',
  status: 'fulfilled',
  delivery_state: 'available',
  created_at: '2026-07-28T12:00:00Z',
  updated_at: '2026-07-28T12:00:00Z',
  ...overrides,
});

describe('RequestCard', () => {
  it('renders the cover, title, author, status and accessible label', () => {
    const html = renderToStaticMarkup(<RequestCard request={request()} />);

    expect(html).toContain('Een titel');
    expect(html).toContain('Een auteur');
    expect(html).toContain('Beschikbaar');
    expect(html).toContain('aria-label="Een titel: Beschikbaar"');
    expect(html).toContain('src="/covers/42.jpg"');
  });

  it('uses the bounded responsive card and cover layout for long content', () => {
    const longTitle = 'Een uitzonderlijk lange boektitel '.repeat(8);
    const longAuthor = 'Een uitzonderlijk lange auteursnaam '.repeat(5);
    const html = renderToStaticMarkup(
      <RequestCard request={request({ title: longTitle, author: longAuthor })} />,
    );

    expect(html).toContain('min-h-36 w-full max-w-[19rem]');
    expect(html).toContain('sm:max-w-[23rem]');
    expect(html).toContain('shrink-0 snap-start overflow-hidden');
    expect(html).toContain('line-clamp-2');
    expect(html).toContain('wrap-break-word');
    expect(html).toContain('mt-auto inline-flex max-w-full');
    expect(html).toContain(longTitle);
    expect(html).toContain(longAuthor);
  });

  it('renders a controlled title fallback when the cover is missing', () => {
    const html = renderToStaticMarkup(
      <RequestCard request={request({ cover_url: null, title: 'Boek zonder omslag' })} />,
    );

    expect(html).not.toContain('<img');
    expect(html).toContain('line-clamp-4 wrap-break-word');
    expect(html).toContain('Boek zonder omslag');
  });
});

describe('wide DashboardShelf skeleton', () => {
  it('matches the loaded request card bounds without widening the page', () => {
    const html = renderToStaticMarkup(
      <DashboardShelf
        id="requests-heading"
        title="Recente aanvragen"
        itemCount={0}
        loading
        emptyMessage="Geen aanvragen"
        onRetry={() => undefined}
        skeletonVariant="wide"
      >
        {null}
      </DashboardShelf>,
    );

    expect(html).toContain('min-h-36 w-full max-w-[19rem] sm:max-w-[23rem]');
    expect(html).toContain('shrink-0 animate-pulse');
    expect(html).not.toContain('h-32 w-[19rem]');
  });
});
