import { describe, expect, it } from 'vitest';

import {
  canManageRequestProcess,
  requesterName,
  requesterSummary,
} from '../pages/requestsPresentation';

describe('canManageRequestProcess', () => {
  it('keeps request history read-only', () => {
    expect(canManageRequestProcess(true, 'waiting_for_release')).toBe(false);
    expect(canManageRequestProcess(true, 'cancelled')).toBe(false);
  });

  it('offers management actions only for active processes', () => {
    expect(canManageRequestProcess(false, 'waiting_for_release')).toBe(true);
    expect(canManageRequestProcess(false, 'completed')).toBe(false);
    expect(canManageRequestProcess(false, 'cancelled')).toBe(false);
    expect(canManageRequestProcess(false, 'failed')).toBe(false);
  });
});

describe('requester presentation', () => {
  const labels = {
    requestedBy: 'Requested by',
    requestedByCount: 'Requested by {count} requesters',
    unknownRequester: 'Unknown user',
  };

  it('shows a single requester directly by name', () => {
    expect(requesterSummary(['timminater'], labels)).toBe('Requested by timminater');
  });

  it('keeps the count explicit for shared requests', () => {
    expect(requesterSummary(['timminater', 'reader'], labels)).toBe('Requested by 2 requesters');
  });

  it('uses a neutral label for a missing requester', () => {
    expect(requesterName(null, labels.unknownRequester)).toBe('Unknown user');
    expect(requesterSummary(['  '], labels)).toBe('Requested by Unknown user');
  });
});
