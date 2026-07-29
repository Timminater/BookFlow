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
    progress: 0.42,
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
