// oxlint-disable-next-line import/no-unassigned-import
import 'foliate-js/view.js';
import type {
  ReaderAdapter,
  ReaderFile,
  ReaderLocator,
  ReaderPreferences,
  ReaderTocItem,
} from '../types';

type FoliateLocation = {
  cfi?: string;
  fraction?: number;
};

type FoliateTocItem = {
  id?: string | number;
  label?: string;
  href?: string;
  subitems?: FoliateTocItem[];
};

type FoliateTransformDetail = {
  data: unknown;
  name?: string;
  type?: unknown;
};

type FoliateView = HTMLElement & {
  book?: {
    rendition?: {
      layout?: string;
    };
    sections?: Array<{ id?: string }>;
    toc?: FoliateTocItem[];
    transformTarget?: EventTarget;
  };
  renderer?: HTMLElement & {
    next?: () => Promise<void>;
    setStyles?: (css: string) => void;
  };
  open: (file: File) => Promise<void>;
  init: (options: {
    lastLocation?: string | { fraction: number };
    showTextStart?: boolean;
  }) => Promise<void>;
  close: () => void;
  next: () => Promise<void>;
  prev: () => Promise<void>;
  goTo: (target: string | { fraction: number }) => Promise<unknown>;
  getCFI?: (index: number) => string;
  lastLocation?: FoliateLocation;
};

const fixedLayoutFallbackCss = `
  html, body {
    box-sizing: border-box !important;
    width: 100% !important;
    height: 100% !important;
    margin: 0 !important;
    overflow: hidden !important;
  }
  body {
    display: grid !important;
    place-items: center !important;
  }
  img, svg, video {
    display: block !important;
    max-width: 100% !important;
    max-height: 100% !important;
    width: auto !important;
    height: auto !important;
    object-fit: contain !important;
  }
`;

const optionalResourceType = (type: string, isSpineResource: boolean): boolean =>
  type.startsWith('audio/') ||
  type.startsWith('font/') ||
  (type.startsWith('image/') && (type !== 'image/svg+xml' || !isSpineResource)) ||
  type.startsWith('video/') ||
  [
    'application/font-sfnt',
    'application/font-woff',
    'application/vnd.ms-fontobject',
    'application/x-font-opentype',
    'application/x-font-ttf',
  ].includes(type);

const resourceType = async (value: unknown): Promise<string> => {
  try {
    const resolved = await Promise.resolve(value);
    return typeof resolved === 'string' ? resolved.split(';', 1)[0].trim().toLowerCase() : '';
  } catch {
    return '';
  }
};

const fileName = (file: ReaderFile): string => {
  const withoutControls = Array.from(file.title, (character) =>
    character.charCodeAt(0) < 32 ? '_' : character,
  ).join('');
  const safeTitle = withoutControls.replace(/[<>:"/\\|?*]/g, '_').slice(0, 120);
  return `${safeTitle || 'book'}.${file.format.toLowerCase()}`;
};

const tocItems = (items: FoliateTocItem[] | undefined, prefix = 'toc'): ReaderTocItem[] =>
  (items ?? []).map((item, index) => ({
    id: String(item.id ?? `${prefix}-${index}`),
    label: item.label?.trim() || `Section ${index + 1}`,
    href: item.href,
    children: tocItems(item.subitems, `${prefix}-${index}`),
  }));

const locatorFromLocation = (location: FoliateLocation | undefined): ReaderLocator | null => {
  if (!location) return null;
  const progression = Math.max(0, Math.min(1, Number(location.fraction) || 0));
  return {
    value: location.cfi || `fraction:${progression}`,
    progression,
  };
};

const readerCss = (preferences: ReaderPreferences): string => {
  let family = 'Georgia, "Times New Roman", serif';
  if (preferences.fontFamily === 'sans-serif') {
    family = 'system-ui, sans-serif';
  } else if (preferences.fontFamily === 'opendyslexic') {
    family = 'OpenDyslexic, system-ui, sans-serif';
  }
  let colors = 'color: #111827 !important; background: #ffffff !important;';
  if (preferences.theme === 'dark') {
    colors = 'color: #e5e7eb !important; background: #020617 !important;';
  } else if (preferences.theme === 'sepia') {
    colors = 'color: #433422 !important; background: #f4ecd8 !important;';
  }
  return `
    :root {
      font-family: ${family} !important;
      font-size: ${preferences.fontSize}% !important;
      line-height: ${preferences.lineHeight} !important;
      ${colors}
    }
    body { font-family: inherit !important; line-height: inherit !important; ${colors} }
    img, svg, video { max-width: 100% !important; height: auto !important; }
  `;
};

class FoliateAdapter implements ReaderAdapter {
  readonly kind;
  private view: FoliateView | null = null;
  private locator: ReaderLocator | null = null;
  private listeners = new Set<(locator: ReaderLocator) => void>();
  private errorListeners = new Set<(error: Error) => void>();
  private relocateHandler: EventListener | null = null;
  private transformHandler: EventListener | null = null;
  private resourceError: Error | null = null;

  constructor(kind: ReaderAdapter['kind']) {
    this.kind = kind;
  }

  async open(container: HTMLElement, file: ReaderFile): Promise<void> {
    const response = await fetch(file.contentUrl, { credentials: 'include' });
    if (!response.ok) {
      throw new Error(`The book file could not be loaded (${response.status}).`);
    }
    const blob = await response.blob();
    const source = new File([blob], fileName(file), {
      type: response.headers.get('Content-Type') || blob.type,
    });
    // The custom element is registered by foliate-js above.
    // oxlint-disable-next-line typescript/no-unsafe-type-assertion
    const view = document.createElement('foliate-view') as FoliateView;
    view.className = 'block h-full w-full';
    // CustomEvent.detail is part of foliate's public relocate event contract.
    // oxlint-disable-next-line typescript/no-unsafe-type-assertion
    this.relocateHandler = ((event: CustomEvent<FoliateLocation>) => {
      const locator = locatorFromLocation(event.detail);
      if (!locator) return;
      this.locator = locator;
      for (const listener of this.listeners) listener(locator);
    }) as EventListener;
    view.addEventListener('relocate', this.relocateHandler);
    container.replaceChildren(view);
    this.view = view;
    try {
      await view.open(source);

      // EPUB resources are transformed lazily. Degrade optional assets without
      // hiding required document failures. Fixed-layout publications also need
      // a safe fit-to-page stylesheet when their authored CSS cannot be loaded.
      // CustomEvent.detail is part of foliate's public transform event contract.
      // oxlint-disable-next-line typescript/no-unsafe-type-assertion
      this.transformHandler = ((event: CustomEvent<FoliateTransformDetail>) => {
        const detail = event.detail;
        const name = detail.name ?? 'unknown';
        detail.data = Promise.resolve(detail.data).catch(async (caught: unknown) => {
          const type = await resourceType(detail.type);
          const context = `EPUB resource ${name} (${type || 'unknown type'}, file ${file.fileId})`;
          if (type === 'text/css') {
            console.warn(`Failed to load ${context}; using reader fallback styles.`, caught);
            return view.book?.rendition?.layout === 'pre-paginated' ? fixedLayoutFallbackCss : '';
          }
          const isSpineResource = Boolean(
            view.book?.sections?.some((section) => section.id === name),
          );
          if (optionalResourceType(type, isSpineResource)) {
            console.warn(`Failed to load optional ${context}; continuing without it.`, caught);
            return '';
          }
          const error = new Error(`Required ${context} could not be loaded.`, { cause: caught });
          this.resourceError = error;
          this.errorListeners.forEach((listener) => listener(error));
          throw error;
        });
      }) as EventListener;
      view.book?.transformTarget?.addEventListener('data', this.transformHandler);

      // Foliate's reference reader starts the first section without awaiting
      // the iframe load. Awaiting View.init() can hang the complete React reader
      // when Chromium delivers a ResizeObserver callback during iframe navigation.
      const firstRender = view.renderer?.next?.();
      if (firstRender) {
        try {
          await this.navigateWithResourceErrors(() => firstRender);
        } catch (caught: unknown) {
          throw new Error('The first EPUB section could not be rendered.', { cause: caught });
        }
      }
    } catch (caught: unknown) {
      this.close();
      throw caught;
    }
  }

  private async navigateWithResourceErrors<T>(action: () => Promise<T>): Promise<T> {
    this.resourceError = null;
    let result: T;
    try {
      result = await action();
    } catch (caught: unknown) {
      const error = this.takeResourceError();
      throw (
        error ??
        (caught instanceof Error ? caught : new Error('EPUB navigation failed.', { cause: caught }))
      );
    }
    const error = this.takeResourceError();
    if (error) throw error;
    return result;
  }

  private takeResourceError(): Error | null {
    const error = this.resourceError;
    this.resourceError = null;
    return error;
  }

  close(): void {
    if (this.view && this.relocateHandler) {
      this.view.removeEventListener('relocate', this.relocateHandler);
    }
    if (this.view?.book?.transformTarget && this.transformHandler) {
      this.view.book.transformTarget.removeEventListener('data', this.transformHandler);
    }
    this.view?.close();
    this.view?.remove();
    this.view = null;
    this.relocateHandler = null;
    this.transformHandler = null;
    this.resourceError = null;
    this.listeners.clear();
    this.errorListeners.clear();
  }

  async next(): Promise<void> {
    const view = this.view;
    if (view) await this.navigateWithResourceErrors(() => view.next());
  }

  async previous(): Promise<void> {
    const view = this.view;
    if (view) await this.navigateWithResourceErrors(() => view.prev());
  }

  async goTo(locator: string): Promise<void> {
    const view = this.view;
    if (!view) return;
    let resolved: unknown;
    if (locator.startsWith('fraction:')) {
      const fraction = Number(locator.slice('fraction:'.length));
      if (Number.isFinite(fraction)) {
        resolved = await this.navigateWithResourceErrors(() => view.goTo({ fraction }));
      } else {
        resolved = await this.navigateWithResourceErrors(() => view.goTo(locator));
      }
    } else {
      resolved = await this.navigateWithResourceErrors(() => view.goTo(locator));
    }
    this.publishFixedLayoutTarget(view, resolved);
  }

  private publishFixedLayoutTarget(view: FoliateView, resolved: unknown): void {
    if (view.book?.rendition?.layout !== 'pre-paginated' || !resolved) return;
    const index = (resolved as { index?: unknown }).index;
    const totalPages = view.book.sections?.length ?? 0;
    if (typeof index !== 'number' || totalPages < 1) return;
    const currentPage = Math.max(1, Math.min(totalPages, index + 1));
    const locator: ReaderLocator = {
      value: view.getCFI?.(index) ?? `fraction:${currentPage / totalPages}`,
      progression: currentPage / totalPages,
      currentPage,
      totalPages,
    };
    this.locator = locator;
    this.listeners.forEach((listener) => listener(locator));
  }

  setPreferences(preferences: ReaderPreferences): void {
    const renderer = this.view?.renderer;
    if (!renderer) return;
    renderer.setAttribute('flow', preferences.flow);
    renderer.setAttribute('max-column-count', preferences.spread === 'double' ? '2' : '1');
    renderer.setStyles?.(readerCss(preferences));
  }

  getLocator(): ReaderLocator | null {
    return this.locator ?? locatorFromLocation(this.view?.lastLocation);
  }

  getToc(): ReaderTocItem[] {
    return tocItems(this.view?.book?.toc);
  }

  subscribe(listener: (locator: ReaderLocator) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  subscribeErrors(listener: (error: Error) => void): () => void {
    this.errorListeners.add(listener);
    return () => this.errorListeners.delete(listener);
  }
}

export const createFoliateAdapter = (kind: ReaderAdapter['kind']): Promise<ReaderAdapter> =>
  Promise.resolve(new FoliateAdapter(kind));
