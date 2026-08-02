export type ReaderKind = 'reflowable' | 'fixed';

export type ReaderTheme = 'light' | 'dark' | 'sepia';
export type ReaderFlow = 'paginated' | 'scrolled';

export interface ReaderFile {
  bookId: number;
  /** ID from V1BookFile, not the legacy download history id. */
  fileId: number;
  title: string;
  author?: string | null;
  format: string;
  contentUrl: string;
  /** Value supplied by the API when the file changed. */
  etag?: string | null;
}

export interface ReaderTocItem {
  id: string;
  label: string;
  href?: string;
  children?: ReaderTocItem[];
}

export interface ReaderLocator {
  /** EPUB CFI for reflowable books, page number for fixed-layout books. */
  value: string;
  progression: number;
  /** Exact fixed-layout page data for presentation; never required for synchronization. */
  currentPage?: number;
  totalPages?: number;
}

export interface ReaderProgress extends ReaderLocator {
  completed: boolean;
  updatedAt?: string;
  /** File to which a server-side locator belongs, when known. */
  fileId?: number | null;
}

export interface ReaderPreferences {
  theme: ReaderTheme;
  fontFamily: 'serif' | 'sans-serif' | 'opendyslexic';
  fontSize: number;
  lineHeight: number;
  flow: ReaderFlow;
  spread: 'single' | 'double';
}

export const defaultReaderPreferences: ReaderPreferences = {
  theme: 'light',
  fontFamily: 'serif',
  fontSize: 100,
  lineHeight: 1.6,
  flow: 'paginated',
  spread: 'single',
};

export interface ReaderAdapter {
  readonly kind: ReaderKind;
  open(container: HTMLElement, file: ReaderFile): Promise<void>;
  close(): void;
  next(): Promise<void> | void;
  previous(): Promise<void> | void;
  goTo(locator: string): Promise<void> | void;
  setPreferences(preferences: ReaderPreferences): Promise<void> | void;
  getLocator(): ReaderLocator | null;
  getToc(): ReaderTocItem[];
  subscribe(listener: (locator: ReaderLocator) => void): () => void;
  subscribeErrors?(listener: (error: Error) => void): () => void;
}
