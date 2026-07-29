const safeFileName = (title: string, format: string): string => {
  const withoutControls = Array.from(title, (character) =>
    character.charCodeAt(0) < 32 ? '_' : character,
  ).join('');
  const safeTitle = withoutControls
    .replace(/[<>:"/\\|?*]/g, '_')
    .trim()
    .slice(0, 120);
  return `${safeTitle || 'book'}.${format.toLowerCase()}`;
};

export const bookFileFromBlob = (
  blob: Blob,
  title: string,
  format: string,
  contentType?: string | null,
): File =>
  new File([blob], safeFileName(title, format), {
    type: contentType || blob.type || 'application/octet-stream',
  });

export const fetchBookFile = async (url: string, title: string, format: string): Promise<File> => {
  const response = await fetch(url, { credentials: 'include' });
  if (!response.ok)
    throw new Error(`Het boekbestand kon niet worden geladen (${response.status}).`);
  const blob = await response.blob();
  return bookFileFromBlob(blob, title, format, response.headers.get('Content-Type'));
};

export const downloadBookFile = (file: File): void => {
  const url = URL.createObjectURL(file);
  const link = document.createElement('a');
  link.href = url;
  link.download = file.name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
};

/**
 * Starts Web Share immediately. Call this only with a File that was prepared
 * before the user tapped: awaiting even an already-resolved file promise can
 * make Android Chrome discard the transient user activation.
 */
export const shareBookFile = (file: File, title: string): Promise<void> => {
  if (typeof navigator.share !== 'function') {
    throw new Error('Openen in een andere app wordt niet ondersteund door deze browser.');
  }
  const shareData: ShareData = { files: [file], title };
  if (typeof navigator.canShare === 'function' && !navigator.canShare(shareData)) {
    throw new Error('Deze browser kan dit boekformaat niet met een andere app delen.');
  }
  return navigator.share(shareData);
};
