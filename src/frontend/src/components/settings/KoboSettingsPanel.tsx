import { useCallback, useState } from 'react';

import { useLanguage } from '../../contexts/LanguageContext';
import { useMountEffect } from '../../hooks/useMountEffect';
import { v1Api } from '../../services/v1Api';
import type { V1KoboSettings } from '../../types/v1';
import { ToggleSwitch } from '../shared/ToggleSwitch';

interface KoboSettingsPanelProps {
  onShowToast?: (message: string, type: 'success' | 'error' | 'info') => void;
  onEnabledChange?: (enabled: boolean) => void;
}

const formatDateTime = (value: string | null, locale: string, never: string): string => {
  if (!value) return never;
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed);
};

export const KoboSettingsPanel = ({ onShowToast, onEnabledChange }: KoboSettingsPanelProps) => {
  const { language, t } = useLanguage();
  const [settings, setSettings] = useState<V1KoboSettings | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isUpdatingEnabled, setIsUpdatingEnabled] = useState(false);
  const [revokingTokenId, setRevokingTokenId] = useState<number | null>(null);
  const [pendingRevokeId, setPendingRevokeId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const nextSettings = await v1Api.koboSettings();
      setSettings(nextSettings);
      onEnabledChange?.(nextSettings.enabled);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : t('koboPanel.loadFailed'));
    } finally {
      setIsLoading(false);
    }
  }, [onEnabledChange, t]);

  useMountEffect(() => {
    void load();
  });

  const updateEnabled = async (enabled: boolean) => {
    if (!settings) return;
    setIsUpdatingEnabled(true);
    setError(null);
    try {
      await v1Api.setKoboEnabled(enabled);
      setSettings((current) => (current ? { ...current, enabled } : current));
      onEnabledChange?.(enabled);
      onShowToast?.(
        enabled ? t('koboPanel.enabledToast') : t('koboPanel.disabledToast'),
        'success',
      );
    } catch (updateError) {
      const message =
        updateError instanceof Error ? updateError.message : t('koboPanel.updateFailed');
      setError(message);
      onShowToast?.(message, 'error');
    } finally {
      setIsUpdatingEnabled(false);
    }
  };

  const revokeToken = async (tokenId: number) => {
    setRevokingTokenId(tokenId);
    setError(null);
    try {
      await v1Api.revokeKoboToken(tokenId);
      setSettings((current) =>
        current
          ? { ...current, tokens: current.tokens.filter((token) => token.id !== tokenId) }
          : current,
      );
      setPendingRevokeId(null);
      onShowToast?.(t('koboPanel.revokedToast'), 'success');
    } catch (revokeError) {
      const message =
        revokeError instanceof Error ? revokeError.message : t('koboPanel.revokeFailed');
      setError(message);
      onShowToast?.(message, 'error');
    } finally {
      setRevokingTokenId(null);
    }
  };

  if (isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center gap-3 p-8 text-sm opacity-70">
        <svg className="h-5 w-5 animate-spin" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
        </svg>
        {t('koboPanel.loading')}
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center">
        <p className="max-w-md text-sm text-red-500">{error ?? t('koboPanel.loadFailed')}</p>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded-lg border border-(--border-muted) bg-(--bg-soft) px-4 py-2 text-sm font-medium hover:bg-(--hover-surface)"
        >
          {t('common.retry')}
        </button>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 p-5 sm:p-8">
        <div>
          <h3 className="text-lg font-semibold">{t('koboPanel.title')}</h3>
          <p className="mt-1 text-sm opacity-65">{t('koboPanel.subtitle')}</p>
        </div>

        {error && (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-600 dark:text-red-300">
            {error}
          </div>
        )}

        <section className="rounded-xl border border-(--border-muted) p-4 sm:p-5">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h4 className="font-medium">{t('koboPanel.enable')}</h4>
              <p className="mt-1 text-sm opacity-65">{t('koboPanel.enableHelp')}</p>
            </div>
            <ToggleSwitch
              checked={settings.enabled}
              onChange={(enabled) => void updateEnabled(enabled)}
              disabled={isUpdatingEnabled}
              color="emerald"
              ariaLabel={t('koboPanel.enable')}
            />
          </div>
        </section>

        {settings.enabled && (
          <section className="rounded-xl border border-(--border-muted) p-4 sm:p-5">
            <div>
              <div>
                <h4 className="font-medium">{t('koboPanel.devices')}</h4>
                <p className="mt-1 text-sm opacity-65">
                  {language === 'nl'
                    ? 'Download hieronder de Windows-patcher. Log in met je BookFlow-account en volg de wizard om je Kobo te koppelen. Handmatige configuratie blijft beschikbaar.'
                    : 'Download the Windows patcher below. Sign in with your BookFlow account and follow the wizard to connect your Kobo. Manual setup is also available.'}
                </p>
              </div>
            </div>

            {settings.tokens.length === 0 ? (
              <p className="mt-5 text-sm opacity-65">{t('koboPanel.noDevices')}</p>
            ) : (
              <ul className="mt-5 divide-y divide-(--border-muted) border-t border-(--border-muted)">
                {settings.tokens.map((token) => (
                  <li
                    key={token.id}
                    className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">
                        {token.device_label || t('koboPanel.unnamedDevice')}
                      </p>
                      <p className="mt-1 font-mono text-xs opacity-60">{token.token_prefix}…</p>
                      <p className="mt-1 text-xs opacity-60">
                        {t('koboPanel.created', {
                          date: formatDateTime(token.created_at, language, t('koboPanel.never')),
                        })}{' '}
                        ·{' '}
                        {t('koboPanel.lastUsed', {
                          date: formatDateTime(token.last_used_at, language, t('koboPanel.never')),
                        })}
                      </p>
                    </div>
                    {pendingRevokeId === token.id ? (
                      <div className="max-w-xs self-start sm:text-right">
                        <p className="text-xs text-red-600">{t('koboPanel.revokeConfirm')}</p>
                        <div className="mt-2 flex gap-2 sm:justify-end">
                          <button
                            type="button"
                            className="rounded-md px-3 py-2 text-sm"
                            onClick={() => setPendingRevokeId(null)}
                            disabled={revokingTokenId !== null}
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void revokeToken(token.id)}
                            disabled={revokingTokenId !== null}
                            className="rounded-md border border-red-500/30 px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            {revokingTokenId === token.id
                              ? t('koboPanel.revoking')
                              : t('koboPanel.confirmRevoke')}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setPendingRevokeId(token.id)}
                        disabled={revokingTokenId !== null}
                        className="self-start rounded-md border border-red-500/30 px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-60 sm:self-auto"
                      >
                        {t('koboPanel.revoke')}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
      </div>
    </div>
  );
};
