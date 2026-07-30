const terminalRequestProcessStatuses = new Set(['completed', 'cancelled', 'failed']);

export interface RequesterLabels {
  requestedBy: string;
  requestedByCount: string;
  unknownRequester: string;
}

/** Keep requester labels consistent without exposing them in reader-facing views. */
export const requesterName = (name: string | null | undefined, unknownRequester: string): string =>
  name?.trim() || unknownRequester;

export const requesterSummary = (
  names: Array<string | null | undefined>,
  labels: RequesterLabels,
): string => {
  if (names.length === 1) {
    return `${labels.requestedBy} ${requesterName(names[0], labels.unknownRequester)}`;
  }
  return labels.requestedByCount.replace('{count}', String(names.length));
};

export const canManageRequestProcess = (
  history: boolean,
  status: string | null | undefined,
): boolean => !history && Boolean(status) && !terminalRequestProcessStatuses.has(status ?? '');
