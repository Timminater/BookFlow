// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { MultiSelectField } from '../components/settings/fields/MultiSelectField';
import { LanguageProvider } from '../contexts/LanguageContext';
import type { MultiSelectFieldConfig } from '../types/settings';

describe('multi-select variants', () => {
  it('can switch between pills and dropdown without changing hook order', () => {
    const field: MultiSelectFieldConfig = {
      key: 'formats',
      label: 'Formats',
      type: 'MultiSelectField',
      value: [],
      options: [{ value: 'epub', label: 'EPUB' }],
      variant: 'pills',
    };
    const onChange = vi.fn();
    const view = render(
      <LanguageProvider>
        <MultiSelectField field={field} value={[]} onChange={onChange} />
      </LanguageProvider>,
    );
    expect(screen.getByText('EPUB')).toBeTruthy();
    view.rerender(
      <LanguageProvider>
        <MultiSelectField
          field={{ ...field, variant: 'dropdown' }}
          value={[]}
          onChange={onChange}
        />
      </LanguageProvider>,
    );
    view.rerender(
      <LanguageProvider>
        <MultiSelectField field={field} value={[]} onChange={onChange} />
      </LanguageProvider>,
    );
    expect(screen.getByText('EPUB')).toBeTruthy();
  });
});
