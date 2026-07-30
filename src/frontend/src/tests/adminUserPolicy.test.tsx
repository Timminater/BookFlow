// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { INITIAL_CREATE_FORM } from '../components/settings/users/types';
import { UserCreateCard } from '../components/settings/users/UserCard';
import { LanguageProvider } from '../contexts/LanguageContext';

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

it('shows and updates the request auto-approval policy when creating a user', () => {
  const onChange = vi.fn();
  render(
    <LanguageProvider>
      <UserCreateCard
        form={INITIAL_CREATE_FORM}
        onChange={onChange}
        creating={false}
        isFirstUser={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />
    </LanguageProvider>,
  );

  expect(screen.getByText('Automatically approve own requests')).toBeTruthy();
  expect(screen.getByText('This does not grant administrator permissions.')).toBeTruthy();
  fireEvent.click(screen.getByRole('switch'));

  expect(onChange).toHaveBeenCalledWith({
    ...INITIAL_CREATE_FORM,
    auto_approve_requests: true,
  });
});

it('provides the Dutch policy label and security explanation', () => {
  window.localStorage.setItem('bookflow-language', 'nl');
  render(
    <LanguageProvider>
      <UserCreateCard
        form={INITIAL_CREATE_FORM}
        onChange={vi.fn()}
        creating={false}
        isFirstUser={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />
    </LanguageProvider>,
  );

  expect(screen.getByText('Eigen verzoeken automatisch goedkeuren')).toBeTruthy();
  expect(screen.getByText('Dit geeft geen administratorrechten.')).toBeTruthy();
});
