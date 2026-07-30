// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigate, type NavigateFunction } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Header } from '../components/Header';
import { LanguageProvider } from '../contexts/LanguageContext';

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: () => ({
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    matches: false,
  }),
});

let navigateFromTest: NavigateFunction | undefined;

const LocationProbe = () => {
  const location = useLocation();
  navigateFromTest = useNavigate();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
};

const renderHeader = () =>
  render(
    <MemoryRouter>
      <LanguageProvider>
        <Header />
        <LocationProbe />
      </LanguageProvider>
    </MemoryRouter>,
  );

afterEach(() => {
  cleanup();
  navigateFromTest = undefined;
});

const requestUrl = (input: RequestInfo | URL): string => {
  if (input instanceof Request) return input.url;
  if (input instanceof URL) return input.href;
  return input;
};

describe('global header search', () => {
  it('debounces a local request and exposes keyboard navigation plus an external action', async () => {
    const originalFetch = globalThis.fetch;
    const calls: string[] = [];
    globalThis.fetch = async (input) => {
      calls.push(requestUrl(input));
      return new Response(
        JSON.stringify({
          limit: 8,
          items: [
            {
              id: 14,
              title: 'Trending title',
              author: 'An Author',
              cover_url: null,
              publish_year: 2026,
              isbn_13: null,
              catalog_visible: false,
              has_imported_file: false,
              in_library: false,
              requested: false,
              is_trending: true,
            },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    };
    try {
      renderHeader();
      const input = screen.getByRole('combobox', { name: /search/i });
      fireEvent.change(input, { target: { value: 'trend' } });
      await waitFor(() => expect(calls).toHaveLength(1), { timeout: 1000 });
      expect(calls[0]).toContain('/api/v1/search/books?q=trend&limit=8');
      expect(await screen.findByText('Trending title')).toBeTruthy();
      expect(screen.getByText('Trending')).toBeTruthy();
      fireEvent.keyDown(input, { key: 'ArrowDown' });
      const resultOption = screen.getByRole('option', { name: /Trending title/i });
      expect(resultOption.getAttribute('aria-selected')).toBe('true');
      expect(resultOption.id).toBe('global-book-search-result-14');
      expect(input.getAttribute('aria-activedescendant')).toBe(resultOption.id);
      fireEvent.keyDown(input, { key: 'ArrowUp' });
      expect(input.getAttribute('aria-activedescendant')).toBe('global-book-search-external');
      fireEvent.keyDown(input, { key: 'ArrowUp' });
      expect(input.getAttribute('aria-activedescendant')).toBe(resultOption.id);
      fireEvent.keyDown(input, { key: 'End' });
      expect(input.getAttribute('aria-activedescendant')).toBe('global-book-search-external');
      fireEvent.keyDown(input, { key: 'Home' });
      expect(input.getAttribute('aria-activedescendant')).toBe(resultOption.id);
      expect(screen.getByRole('listbox').className).toContain('max-h-[60vh]');
      expect(screen.getByRole('listbox').className).toContain('w-full');
      fireEvent.keyDown(input, { key: 'Enter' });
      expect(screen.getByTestId('location').textContent).toBe('/suggestions/overall/14');
      expect(input.getAttribute('aria-activedescendant')).toBeNull();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('does not request short input', () => {
    const spy = vi.fn();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = spy;
    try {
      renderHeader();
      fireEvent.change(screen.getByRole('combobox', { name: /search/i }), {
        target: { value: 'a' },
      });
      expect(spy).not.toHaveBeenCalled();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('keeps the exact query for explicit external search and normal submit fallback', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () =>
      new Response(JSON.stringify({ limit: 8, items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    try {
      renderHeader();
      const input = screen.getByRole('combobox', { name: /search/i });
      fireEvent.change(input, { target: { value: 'Dune Messiah' } });
      const external = await screen.findByRole('option', {
        name: /Search externally for “Dune Messiah”/i,
      });
      expect(external.id).toBe('global-book-search-external');
      fireEvent.keyDown(input, { key: 'ArrowDown' });
      expect(input.getAttribute('aria-activedescendant')).toBe('global-book-search-external');
      fireEvent.change(input, { target: { value: 'Dune Messiah revised' } });
      expect(input.getAttribute('aria-activedescendant')).toBeNull();
      fireEvent.change(input, { target: { value: 'Dune Messiah' } });
      await screen.findByRole('option', { name: /Search externally for “Dune Messiah”/i });
      fireEvent.keyDown(input, { key: 'ArrowDown' });
      fireEvent.keyDown(input, { key: 'Escape' });
      await waitFor(() => expect(input.getAttribute('aria-activedescendant')).toBeNull());
      fireEvent.focus(input);
      expect(await screen.findByRole('option', { name: /Search externally/i })).toBeTruthy();
      fireEvent.keyDown(input, { key: 'ArrowDown' });
      fireEvent.mouseDown(document.body);
      await waitFor(() => expect(input.getAttribute('aria-activedescendant')).toBeNull());
      fireEvent.focus(input);
      fireEvent.keyDown(input, { key: 'ArrowDown' });
      await act(async () => navigateFromTest?.('/library?audit=1'));
      expect(screen.getByTestId('location').textContent).toBe('/library?audit=1');
      expect(input.getAttribute('aria-expanded')).toBe('false');
      expect(input.getAttribute('aria-activedescendant')).toBeNull();
      fireEvent.focus(input);
      const reopenedExternal = await screen.findByRole('option', {
        name: /Search externally for “Dune Messiah”/i,
      });
      fireEvent.click(reopenedExternal);
      expect(screen.getByTestId('location').textContent).toBe(
        '/catalog?q=Dune%20Messiah&external=1',
      );
      expect(input.getAttribute('aria-activedescendant')).toBeNull();

      fireEvent.change(input, { target: { value: 'Foundation' } });
      const form = input.closest('form');
      expect(form).not.toBeNull();
      if (form) fireEvent.submit(form);
      expect(screen.getByTestId('location').textContent).toBe('/catalog?q=Foundation');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('ignores an older response after the query changes', async () => {
    const originalFetch = globalThis.fetch;
    let resolveOld: ((response: Response) => void) | undefined;
    globalThis.fetch = (input) => {
      const url = requestUrl(input);
      if (url.includes('q=old')) {
        return new Promise<Response>((resolve) => {
          resolveOld = resolve;
        });
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({
            limit: 8,
            items: [
              {
                id: 2,
                title: 'New result',
                author: null,
                cover_url: null,
                publish_year: null,
                isbn_13: null,
                catalog_visible: true,
                has_imported_file: true,
                in_library: false,
                requested: false,
                is_trending: false,
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );
    };
    try {
      renderHeader();
      const input = screen.getByRole('combobox', { name: /search/i });
      fireEvent.change(input, { target: { value: 'old' } });
      await waitFor(() => expect(resolveOld).toBeTypeOf('function'));
      fireEvent.change(input, { target: { value: 'new' } });
      expect(await screen.findByText('New result')).toBeTruthy();
      resolveOld?.(
        new Response(
          JSON.stringify({
            limit: 8,
            items: [{ id: 1, title: 'Old result', author: null }],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      );
      await Promise.resolve();
      expect(screen.queryByText('Old result')).toBeNull();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
