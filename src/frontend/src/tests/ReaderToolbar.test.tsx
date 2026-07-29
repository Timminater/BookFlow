// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LanguageProvider } from '../contexts/LanguageContext';
import { ReaderToolbar } from '../reader/components/ReaderToolbar';

const preferences = {
  theme: 'light',
  fontFamily: 'serif',
  fontSize: 100,
  lineHeight: 1.5,
  flow: 'paginated',
  spread: 'single',
} as const;

const renderToolbar = (overrides: Partial<React.ComponentProps<typeof ReaderToolbar>> = {}) => {
  const props: React.ComponentProps<typeof ReaderToolbar> = {
    title: 'Een uitzonderlijk lange Nederlandstalige boektitel voor een kleine telefoon',
    controlsVisible: true,
    position: { value: 'page:36', progression: 0.18, currentPage: 36, totalPages: 200 },
    preferences,
    onBack: vi.fn(),
    onToggleToc: vi.fn(),
    onToggleSettings: vi.fn(),
    onToggleFullscreen: vi.fn(),
    onDownload: vi.fn().mockResolvedValue(undefined),
    onShare: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  render(
    <LanguageProvider>
      <ReaderToolbar {...props} />
    </LanguageProvider>,
  );
  return props;
};

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('ReaderToolbar', () => {
  it('keeps the primary mobile controls named and all desktop actions available', () => {
    renderToolbar();

    expect(screen.getByLabelText('Reading progress').textContent).toBe('36/200 (18%)');
    expect(screen.getAllByRole('button', { name: 'Close reader' })[0]).toHaveProperty(
      'title',
      'Close reader',
    );
    expect(
      screen.getByRole('button', { name: 'More reader actions' }).getAttribute('aria-expanded'),
    ).toBe('false');
    for (const name of ['Download', 'Open in another app', 'Contents', 'Display', 'Fullscreen']) {
      expect(screen.getByRole('button', { name })).toBeTruthy();
    }
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('uses the Dutch progress label without changing the numeric presentation', () => {
    window.localStorage.setItem('bookflow-language', 'nl');
    renderToolbar();

    expect(screen.getByLabelText('Leesvoortgang').textContent).toBe('36/200 (18%)');
    expect(screen.getByRole('button', { name: 'Meer readeracties' })).toBeTruthy();
  });

  it('shows percentage-only progress for a reflowable EPUB', () => {
    renderToolbar({
      position: { value: 'epubcfi(/6/4)', progression: 0.18 },
    });

    expect(screen.getByLabelText('Reading progress').textContent).toBe('18%');
  });

  it('keeps the compact row bounded without a horizontally scrolling toolbar', () => {
    renderToolbar();

    const progress = screen.getByLabelText('Reading progress');
    const row = progress.parentElement;
    const toolbar = progress.closest('header');
    expect(row?.className).toContain('min-w-0');
    expect(row?.querySelector('p')?.className).toContain('min-w-0 flex-1 truncate');
    expect(progress.className).toContain('shrink-0');
    expect(toolbar?.className).not.toContain('overflow-x-auto');
  });

  it('opens the mobile action menu, focuses it, supports arrow keys, and invokes actions', () => {
    const props = renderToolbar();
    const trigger = screen.getByRole('button', { name: 'More reader actions' });

    fireEvent.click(trigger);

    const menu = screen.getByRole('menu', { name: 'Reader actions' });
    const items = screen.getAllByRole('menuitem');
    expect(items).toHaveLength(5);
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(menu, { key: 'End' });
    expect(document.activeElement).toBe(items.at(-1));
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(items[0]);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Contents' }));

    expect(props.onToggleToc).toHaveBeenCalledOnce();
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('closes the menu with Escape and restores focus to its trigger', () => {
    renderToolbar();
    const trigger = screen.getByRole('button', { name: 'More reader actions' });
    fireEvent.click(trigger);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByRole('menu')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('reports action failures but ignores an AbortError', async () => {
    const onDownload = vi.fn().mockRejectedValueOnce(new Error('Download mislukt'));
    renderToolbar({ onDownload });
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));

    expect((await screen.findByRole('alert')).textContent).toContain('Download mislukt');

    cleanup();
    const aborted = vi.fn().mockRejectedValueOnce(new DOMException('Cancelled', 'AbortError'));
    renderToolbar({ onDownload: aborted });
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));

    await waitFor(() => expect(aborted).toHaveBeenCalledOnce());
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
