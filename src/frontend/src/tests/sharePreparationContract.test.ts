import { readFile } from 'node:fs/promises';

import { describe, expect, it } from 'vitest';

import { translations } from '../i18n/translations';

const sourceRoot = new URL('..', import.meta.url);
const readSource = (path: string) => readFile(new URL(path, sourceRoot), 'utf8');

describe('share preparation lifecycle', () => {
  it('drops stale A-to-B preparation settles and keeps one prepared File', async () => {
    const source = await readSource('pages/CatalogBookPage.tsx');

    expect(source).toContain('const preparedFileGeneration = useRef(0)');
    expect(source).toContain('preparedFileGeneration.current += 1');
    expect(source).toContain('if (generation !== preparedFileGeneration.current) return');
    expect(source).toContain('if (generation === preparedFileGeneration.current)');
    expect(source).toContain('preparedFiles.current.clear()');
    expect(source).toContain("setPreparedFileStates({ [file.id]: 'preparing' })");
  });

  it('uses English and Dutch preparation messages instead of hard-coded UI text', async () => {
    const [catalog, reader] = await Promise.all([
      readSource('pages/CatalogBookPage.tsx'),
      readSource('reader/ReaderPage.tsx'),
    ]);

    expect(translations.en['catalogDetail.preparingFile']).toBe('Preparing file…');
    expect(translations.nl['catalogDetail.preparingFile']).toBe('Bestand voorbereiden…');
    expect(translations.en['catalogDetail.sharePreparationPending']).toContain('prepared');
    expect(translations.nl['catalogDetail.sharePreparationFailed']).toContain('voorbereid');
    expect(catalog).toContain("t('catalogDetail.sharePreparationPending')");
    expect(catalog).toContain("t('catalogDetail.sharePreparationFailed')");
    expect(reader).toContain("t('catalogDetail.sharePreparationPending')");
    expect(reader).toContain("t('catalogDetail.sharePreparationFailed')");
  });
});
