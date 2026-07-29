import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist';
// oxlint-disable-next-line import/default
import workerUrl from 'pdfjs-dist/build/pdf.worker.mjs?url';

import type {
  ReaderAdapter,
  ReaderFile,
  ReaderLocator,
  ReaderPreferences,
  ReaderTocItem,
} from '../types';

GlobalWorkerOptions.workerSrc = workerUrl;

class PdfAdapter implements ReaderAdapter {
  readonly kind = 'fixed' as const;
  private document: PDFDocumentProxy | null = null;
  private container: HTMLElement | null = null;
  private canvas: HTMLCanvasElement | null = null;
  private page = 1;
  private preferences: ReaderPreferences | null = null;
  private renderTask: RenderTask | null = null;
  private listeners = new Set<(locator: ReaderLocator) => void>();
  private toc: ReaderTocItem[] = [];

  async open(container: HTMLElement, file: ReaderFile): Promise<void> {
    this.container = container;
    const canvas = document.createElement('canvas');
    canvas.className = 'mx-auto block max-h-full max-w-full';
    container.replaceChildren(canvas);
    this.canvas = canvas;
    const task = getDocument({
      url: file.contentUrl,
      withCredentials: true,
      rangeChunkSize: 256 * 1024,
    });
    this.document = await task.promise;
    const outline = await this.document.getOutline();
    this.toc = (outline ?? []).map((item, index) => ({
      id: `pdf-outline-${index}`,
      label: item.title || `Section ${index + 1}`,
    }));
    await this.render();
  }

  close(): void {
    this.renderTask?.cancel();
    this.renderTask = null;
    void this.document?.destroy();
    this.document = null;
    this.canvas?.remove();
    this.canvas = null;
    this.container = null;
    this.listeners.clear();
  }

  async next(): Promise<void> {
    if (!this.document || this.page >= this.document.numPages) return;
    this.page += 1;
    await this.render();
  }

  async previous(): Promise<void> {
    if (this.page <= 1) return;
    this.page -= 1;
    await this.render();
  }

  async goTo(locator: string): Promise<void> {
    if (!this.document) return;
    const fraction = Number(locator.replace(/^fraction:/, ''));
    if (locator.startsWith('fraction:') && Number.isFinite(fraction)) {
      this.page = Math.round(fraction * (this.document.numPages - 1)) + 1;
      await this.render();
      return;
    }
    const requested = Number(locator.replace(/^page:/, ''));
    if (!Number.isInteger(requested)) return;
    this.page = Math.max(1, Math.min(this.document.numPages, requested));
    await this.render();
  }

  async setPreferences(preferences: ReaderPreferences): Promise<void> {
    this.preferences = preferences;
    if (this.document) await this.render();
  }

  getLocator(): ReaderLocator | null {
    if (!this.document) return null;
    return {
      value: `page:${this.page}`,
      progression: this.document.numPages <= 1 ? 1 : (this.page - 1) / (this.document.numPages - 1),
      currentPage: this.page,
      totalPages: this.document.numPages,
    };
  }

  getToc(): ReaderTocItem[] {
    return this.toc;
  }

  subscribe(listener: (locator: ReaderLocator) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private async render(): Promise<void> {
    if (!this.document || !this.canvas || !this.container) return;
    this.renderTask?.cancel();
    const page = await this.document.getPage(this.page);
    const baseViewport = page.getViewport({ scale: 1 });
    const width = Math.max(320, this.container.clientWidth - 24);
    const height = Math.max(320, this.container.clientHeight - 24);
    const fitScale = Math.min(width / baseViewport.width, height / baseViewport.height);
    const preferenceScale = Math.max(0.5, (this.preferences?.fontSize ?? 100) / 100);
    const viewport = page.getViewport({ scale: fitScale * preferenceScale });
    const outputScale = window.devicePixelRatio || 1;
    const context = this.canvas.getContext('2d');
    if (!context) throw new Error('Canvas rendering is not available in this browser.');
    this.canvas.width = Math.floor(viewport.width * outputScale);
    this.canvas.height = Math.floor(viewport.height * outputScale);
    this.canvas.style.width = `${Math.floor(viewport.width)}px`;
    this.canvas.style.height = `${Math.floor(viewport.height)}px`;
    const transform = outputScale === 1 ? undefined : [outputScale, 0, 0, outputScale, 0, 0];
    this.renderTask = page.render({
      canvas: this.canvas,
      canvasContext: context,
      viewport,
      transform,
    });
    try {
      await this.renderTask.promise;
    } catch (error) {
      if (!(error instanceof Error) || error.name !== 'RenderingCancelledException') throw error;
      return;
    }
    const locator = this.getLocator();
    if (locator) for (const listener of this.listeners) listener(locator);
  }
}

export const createPdfAdapter = (): Promise<ReaderAdapter> => Promise.resolve(new PdfAdapter());
