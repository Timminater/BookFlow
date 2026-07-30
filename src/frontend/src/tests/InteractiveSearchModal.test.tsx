// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LanguageProvider } from '../contexts/LanguageContext';
import { InteractiveSearchModal } from '../pages/InteractiveSearchModal';
import { v1Api } from '../services/v1Api';
import type { V1Candidate, V1RequestSearchRun } from '../types/v1';

const run = (
  status: V1RequestSearchRun['status'] = 'completed',
  values: Partial<V1RequestSearchRun> = {},
): V1RequestSearchRun => ({
  id: 11,
  request_process_id: 4,
  status,
  source_count: 2,
  candidate_count: 0,
  errors_json: '{}',
  started_at: '2026-07-30T12:00:00Z',
  completed_at: status === 'queued' || status === 'running' ? null : '2026-07-30T12:01:00Z',
  ...values,
});

const candidate = (id: number, values: Partial<V1Candidate> = {}): V1Candidate => ({
  id,
  request_process_id: 4,
  accepted: 1,
  total_score: 10,
  file_format: 'epub',
  title: `Release ${id}`,
  source: 'Prowlarr',
  language: 'en',
  ...values,
});

const mockApi = (
  status: V1RequestSearchRun['status'] = 'completed',
  candidates: V1Candidate[] = [],
  attempts: Awaited<ReturnType<typeof v1Api.attempts>>['items'] = [],
) => {
  vi.spyOn(v1Api, 'interactiveSearch').mockResolvedValue({
    request_process_id: 4,
    search_run: run(status),
    scheduled: true,
    interactive: true,
  });
  vi.spyOn(v1Api, 'requestSearchRun').mockResolvedValue({ search_run: run(status) });
  vi.spyOn(v1Api, 'candidates').mockResolvedValue({
    items: candidates,
    total: candidates.length,
    offset: 0,
    limit: 50,
  });
  vi.spyOn(v1Api, 'attempts').mockResolvedValue({
    items: attempts,
    total: attempts.length,
    offset: 0,
    limit: 50,
  });
  vi.spyOn(v1Api, 'selectCandidate').mockResolvedValue({
    candidate_id: 99,
    selected: true,
    forced: false,
  });
};

const renderModal = (language: 'en' | 'nl' = 'en') => {
  window.localStorage.setItem('bookflow-language', language);
  const props: React.ComponentProps<typeof InteractiveSearchModal> = {
    requestProcess: {
      id: 4,
      book_id: 8,
      status: 'searching',
      monitoring_enabled: 1,
      next_search_at: null,
      active_attempt_id: null,
    },
    title: 'The Book',
    author: 'The Author',
    profile: 'EPUB NL',
    onClose: vi.fn(),
    onShowToast: vi.fn(),
    onChanged: vi.fn().mockResolvedValue(undefined),
  };
  const rendered = render(
    <LanguageProvider>
      <InteractiveSearchModal {...props} />
    </LanguageProvider>,
  );
  return { ...rendered, props };
};

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('InteractiveSearchModal', () => {
  it.each([
    ['queued', 'Queued'],
    ['partial', 'Partially completed'],
    ['failed', 'Failed'],
    ['completed', 'Completed'],
  ] as const)('renders the localized %s header state', async (status, label) => {
    mockApi(status);
    renderModal();
    expect((await screen.findAllByText(new RegExp(label))).length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { name: 'The Book' })).toBeTruthy();
    expect(screen.getByText('The Author')).toBeTruthy();
    expect(screen.getByText(/EPUB NL/)).toBeTruthy();
  });

  it('localizes candidate fallbacks and every backend attempt state in Dutch', async () => {
    const attemptStates = [
      'queued',
      'submitted',
      'downloading',
      'importing',
      'completed',
      'retry_scheduled',
      'failed',
      'cancelled',
    ] as const;
    mockApi(
      'partial',
      [candidate(1, { source: null, language: null, file_format: null })],
      attemptStates.map((status, index) => ({
        id: index + 1,
        request_process_id: 4,
        status,
        attempt_number: index + 1,
        retry_count: 0,
      })),
    );
    renderModal('nl');

    expect((await screen.findAllByText(/Gedeeltelijk voltooid/)).length).toBeGreaterThan(0);
    expect(screen.getByRole('tab', { name: /Onbekende bron/ })).toBeTruthy();
    expect(screen.getAllByText('Onbekend').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: /Downloadpogingen tonen/ }));
    for (const label of [
      'In wachtrij',
      'Ingediend bij downloadclient',
      'Bezig met downloaden',
      'Bezig met importeren',
      'Voltooid',
      'Nieuwe poging gepland',
      'Mislukt',
      'Geannuleerd',
    ]) {
      expect(screen.getAllByText(new RegExp(label)).length).toBeGreaterThan(0);
    }
  });

  it('uses the localized unknown fallback only for an unrecognized runtime attempt state', async () => {
    const unknownAttempt: Awaited<ReturnType<typeof v1Api.attempts>>['items'][number] = {
      id: 1,
      request_process_id: 4,
      status: 'cancelled',
      attempt_number: 1,
      retry_count: 0,
    };
    Object.defineProperty(unknownAttempt, 'status', { value: 'future_backend_state' });
    mockApi('completed', [], [unknownAttempt]);
    renderModal('nl');
    fireEvent.click(await screen.findByRole('button', { name: /Downloadpogingen tonen/ }));
    expect(screen.getByText(/#1 · Onbekend/)).toBeTruthy();
  });

  it('starts a new durable run and refreshes the current run', async () => {
    mockApi('completed');
    renderModal();
    const searchAgain = await screen.findByRole('button', { name: 'Search again' });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(v1Api.requestSearchRun).toHaveBeenCalledTimes(2));
    fireEvent.click(searchAgain);
    await waitFor(() => expect(v1Api.interactiveSearch).toHaveBeenCalledTimes(2));
  });

  it('selects eligible candidates normally and enforces confirmation plus an audit reason', async () => {
    mockApi('completed', [
      candidate(1),
      candidate(2, { accepted: 0, reject_reasons: ['Wrong format'] }),
    ]);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('');
    renderModal();

    fireEvent.click(await screen.findByRole('button', { name: 'Grab' }));
    await waitFor(() => expect(v1Api.selectCandidate).toHaveBeenCalledWith(1, false, undefined));
    fireEvent.click(screen.getByRole('button', { name: 'Grab with override' }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(prompt).toHaveBeenCalledOnce();
    expect(v1Api.selectCandidate).toHaveBeenCalledTimes(1);

    prompt.mockReturnValue('Reviewed exception');
    fireEvent.click(screen.getByRole('button', { name: 'Grab with override' }));
    await waitFor(() =>
      expect(v1Api.selectCandidate).toHaveBeenLastCalledWith(2, true, 'Reviewed exception'),
    );
  });

  it('stops polling after unmount', async () => {
    mockApi('running');
    const { unmount } = renderModal();
    await waitFor(() => expect(v1Api.requestSearchRun).toHaveBeenCalledOnce());
    unmount();
    vi.useFakeTimers();
    await vi.advanceTimersByTimeAsync(2000);
    expect(v1Api.requestSearchRun).toHaveBeenCalledOnce();
  });

  it('initializes and traps focus, closes on Escape, and restores the prior focus', async () => {
    mockApi();
    const opener = document.createElement('button');
    document.body.append(opener);
    opener.focus();
    const { unmount, props } = renderModal();
    const close = screen.getByRole('button', { name: 'Close interactive search' });
    await waitFor(() => expect(document.activeElement).toBe(close));

    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(
      screen.getByRole('button', { name: /Show download attempts/ }),
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(props.onClose).toHaveBeenCalledOnce();
    unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it('implements roving source tabs with arrows, Home, End and an owned tabpanel', async () => {
    mockApi('completed', [
      candidate(1, { source: 'Prowlarr' }),
      candidate(2, { source: 'Direct' }),
    ]);
    renderModal();
    const all = await screen.findByRole('tab', { name: /^All/ });
    const prowlarr = screen.getByRole('tab', { name: /^Prowlarr/ });
    const direct = screen.getByRole('tab', { name: /^Direct/ });
    all.focus();
    fireEvent.keyDown(all, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(prowlarr);
    expect(prowlarr.getAttribute('aria-selected')).toBe('true');
    fireEvent.keyDown(prowlarr, { key: 'End' });
    expect(document.activeElement).toBe(direct);
    fireEvent.keyDown(direct, { key: 'Home' });
    expect(document.activeElement).toBe(all);
    expect(all.getAttribute('aria-controls')).toBe('interactive-search-results');
    expect(screen.getByRole('tabpanel').id).toBe('interactive-search-results');
  });
});
