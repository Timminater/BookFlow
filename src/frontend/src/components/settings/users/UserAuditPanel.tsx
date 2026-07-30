import { useCallback, useState } from 'react';

import { useLanguage } from '../../../contexts/LanguageContext';
import { useDependencyEffect } from '../../../hooks/useMountEffect';
import {
  getAdminUserAudit,
  revokeAdminUserSession,
  type AdminUser,
  type AdminUserAudit,
} from '../../../services/api';

interface UserAuditPanelProps {
  userId: number;
  user?: AdminUser;
  activeTab?: AuditTab;
  onTabChange?: (tab: AuditTab) => void;
}

export type AuditTab = 'profile' | 'history' | 'downloads' | 'ips' | 'logins' | 'kobo';

const formatDate = (value: string | null, language: 'en' | 'nl') => {
  if (!value) return language === 'nl' ? 'Nooit' : 'Never';
  const normalized = value.endsWith('Z') || value.includes('+') ? value : `${value}Z`;
  return new Date(normalized).toLocaleString(language);
};

const EmptyState = ({ children }: { children: string }) => (
  <p className="rounded-lg border border-dashed border-(--border-muted) p-4 text-sm opacity-60">
    {children}
  </p>
);

export const UserAuditPanel = ({ userId, user, activeTab, onTabChange }: UserAuditPanelProps) => {
  const { language } = useLanguage();
  const text = useCallback((en: string, nl: string) => (language === 'nl' ? nl : en), [language]);
  const [audit, setAudit] = useState<AdminUserAudit | null>(null);
  const [internalTab, setInternalTab] = useState<AuditTab>(user ? 'profile' : 'logins');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<number | null>(null);

  const loadAudit = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setAudit(await getAdminUserAudit(userId));
    } catch (reason: unknown) {
      setError(
        reason instanceof Error
          ? reason.message
          : text('Failed to load audit data', 'Auditgegevens laden mislukt'),
      );
    } finally {
      setLoading(false);
    }
  }, [text, userId]);

  useDependencyEffect(() => {
    void loadAudit();
  }, [loadAudit]);

  const revokeSession = async (sessionId: number) => {
    setRevoking(sessionId);
    try {
      await revokeAdminUserSession(userId, sessionId);
      await loadAudit();
    } catch (reason: unknown) {
      setError(
        reason instanceof Error
          ? reason.message
          : text('Failed to revoke session', 'Sessie intrekken mislukt'),
      );
    } finally {
      setRevoking(null);
    }
  };

  if (loading && !audit) {
    return (
      <p className="text-sm opacity-60">
        {text('Loading account activity…', 'Accountactiviteit laden…')}
      </p>
    );
  }
  if (error && !audit) return <p className="text-sm text-red-600">{error}</p>;
  if (!audit) return null;

  const tab = activeTab ?? internalTab;
  const { authentication, downloads, history, ip_addresses: ipAddresses, kobo } = audit;
  const tabs: Array<[AuditTab, string, number | null]> = [
    ...(user
      ? ([['profile', text('Profile', 'Profiel'), null]] as Array<
          [AuditTab, string, number | null]
        >)
      : []),
    ['history', text('History', 'Historie'), history.length],
    ['downloads', 'Downloads', downloads.recent_downloads.length],
    ['ips', text('IP addresses', 'IP-adressen'), ipAddresses.length],
    [
      'logins',
      text('Logins & sessions', 'Aanmeldingen en sessies'),
      authentication.recent_events.length,
    ],
    ['kobo', text('Kobo sync', 'Kobo-sync'), kobo.recent_syncs.length],
  ];

  return (
    <section className="space-y-4 border-t border-(--border-muted) pt-5">
      <div className="flex flex-wrap gap-2 border-b border-(--border-muted) pb-3">
        {tabs.map(([key, label, count]) => (
          <button
            key={key}
            type="button"
            onClick={() => {
              setInternalTab(key);
              onTabChange?.(key);
            }}
            className={`rounded-md px-3 py-2 text-xs font-medium transition ${
              tab === key ? 'bg-(--accent) text-white' : 'bg-(--bg-soft) hover:opacity-80'
            }`}
          >
            {label}
            {count !== null && <span className="ml-1 opacity-70">{count}</span>}
          </button>
        ))}
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}

      {tab === 'profile' && user && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {[
            [text('Username', 'Gebruikersnaam'), user.username],
            [text('Display name', 'Weergavenaam'), user.display_name || '-'],
            [text('Email address', 'E-mailadres'), user.email || '-'],
            [text('Authentication', 'Authenticatie'), user.auth_source],
            [text('Role', 'Rol'), user.role],
            [
              text('Account status', 'Accountstatus'),
              user.is_active ? text('Active', 'Actief') : text('Inactive', 'Inactief'),
            ],
            [text('Library access', 'Bibliotheektoegang'), user.library_capability],
            [
              text('Automatically approve own requests', 'Eigen verzoeken automatisch goedkeuren'),
              user.auto_approve_requests ? text('Yes', 'Ja') : text('No', 'Nee'),
            ],
            [text('Created', 'Aangemaakt'), formatDate(user.created_at, language)],
          ].map(([label, value]) => (
            <div
              key={label}
              className="rounded-lg border border-(--border-muted) bg-(--bg-soft) p-4"
            >
              <p className="text-[11px] tracking-wide uppercase opacity-50">{label}</p>
              <p className="mt-1 text-sm font-medium break-words">{value}</p>
            </div>
          ))}
        </div>
      )}

      {tab === 'logins' && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h4 className="font-medium">
                {text('Logins & sessions', 'Aanmeldingen en sessies')}
              </h4>
              <p className="mt-1 text-xs opacity-60">
                {text(
                  `${authentication.failed_login_count_48h} failed or blocked login attempts in 48 hours.`,
                  `${authentication.failed_login_count_48h} mislukte of geblokkeerde aanmeldpogingen in 48 uur.`,
                )}
              </p>
            </div>
          </div>

          {authentication.sessions.length === 0 ? (
            <EmptyState>
              {text(
                'No tracked browser sessions have been recorded yet.',
                'Er zijn nog geen gevolgde browsersessies vastgelegd.',
              )}
            </EmptyState>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-(--border-muted)">
              <table className="min-w-full text-left text-xs">
                <thead className="bg-(--bg-soft)">
                  <tr>
                    <th className="px-3 py-2 font-medium">{text('Last seen', 'Laatst gezien')}</th>
                    <th className="px-3 py-2 font-medium">{text('IP address', 'IP-adres')}</th>
                    <th className="px-3 py-2 font-medium">{text('Source', 'Bron')}</th>
                    <th className="px-3 py-2 font-medium">{text('Status', 'Status')}</th>
                    <th className="px-3 py-2" aria-label={text('Actions', 'Acties')} />
                  </tr>
                </thead>
                <tbody className="divide-y divide-(--border-muted)">
                  {authentication.sessions.map((item) => (
                    <tr key={item.id}>
                      <td className="px-3 py-2 whitespace-nowrap">
                        {formatDate(item.last_seen_at, language)}
                      </td>
                      <td className="px-3 py-2 font-mono whitespace-nowrap">{item.source_ip}</td>
                      <td className="px-3 py-2">{item.auth_source}</td>
                      <td className="px-3 py-2">
                        {item.is_active ? text('Active', 'Actief') : text('Ended', 'Beëindigd')}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {item.is_active && (
                          <button
                            type="button"
                            disabled={revoking === item.id}
                            onClick={() => void revokeSession(item.id)}
                            className="text-red-600 hover:underline disabled:opacity-50"
                          >
                            {revoking === item.id
                              ? text('Revoking…', 'Intrekken…')
                              : text('Revoke', 'Intrekken')}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {authentication.recent_events.length > 0 && (
            <div className="overflow-x-auto rounded-lg border border-(--border-muted)">
              <table className="min-w-full text-left text-xs">
                <thead className="bg-(--bg-soft)">
                  <tr>
                    <th className="px-3 py-2 font-medium">{text('Time', 'Tijd')}</th>
                    <th className="px-3 py-2 font-medium">{text('Event', 'Gebeurtenis')}</th>
                    <th className="px-3 py-2 font-medium">{text('Result', 'Resultaat')}</th>
                    <th className="px-3 py-2 font-medium">{text('IP address', 'IP-adres')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-(--border-muted)">
                  {authentication.recent_events.map((item) => (
                    <tr key={item.id}>
                      <td className="px-3 py-2 whitespace-nowrap">
                        {formatDate(item.created_at, language)}
                      </td>
                      <td className="px-3 py-2">{item.event_type.replace('_', ' ')}</td>
                      <td className="px-3 py-2">{item.outcome}</td>
                      <td className="px-3 py-2 font-mono whitespace-nowrap">{item.source_ip}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {tab === 'history' &&
        (history.length === 0 ? (
          <EmptyState>
            {text(
              'No completed download activity has been recorded for this user.',
              'Er is nog geen voltooide downloadactiviteit voor deze gebruiker vastgelegd.',
            )}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-(--border-muted)">
            <table className="min-w-full text-left text-xs">
              <thead className="bg-(--bg-soft)">
                <tr>
                  <th className="px-3 py-2 font-medium">{text('Time', 'Tijd')}</th>
                  <th className="px-3 py-2 font-medium">{text('Title', 'Titel')}</th>
                  <th className="px-3 py-2 font-medium">{text('Format', 'Formaat')}</th>
                  <th className="px-3 py-2 font-medium">{text('Result', 'Resultaat')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-(--border-muted)">
                {history.map((item) => (
                  <tr key={item.id}>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {formatDate(item.terminal_at, language)}
                    </td>
                    <td className="px-3 py-2">
                      {item.title}
                      {item.author ? <span className="block opacity-60">{item.author}</span> : null}
                    </td>
                    <td className="px-3 py-2">{item.format || item.content_type || '-'}</td>
                    <td className="px-3 py-2">{item.final_status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}

      {tab === 'downloads' && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h4 className="font-medium">{text('File downloads', 'Bestandsdownloads')}</h4>
              <p className="mt-1 text-xs opacity-60">
                {text(
                  'Successful file deliveries are logged. A warning starts after three consecutive IP switches, with no more than 48 hours between switches.',
                  'Geslaagde bestandsleveringen worden vastgelegd. Na drie opeenvolgende IP-wisselingen binnen telkens 48 uur verschijnt een waarschuwing.',
                )}
              </p>
            </div>
            <span
              className={`rounded-full px-2.5 py-1 text-xs font-medium ${
                downloads.suspicious
                  ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300'
                  : 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
              }`}
            >
              {downloads.suspicious
                ? text('Suspicious activity', 'Verdachte activiteit')
                : text('No download warning', 'Geen downloadwaarschuwing')}
            </span>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              [
                text('Downloads (14 days)', 'Downloads (14 dagen)'),
                String(downloads.download_count),
              ],
              [
                text('IP switch streak', 'Reeks IP-wisselingen'),
                String(downloads.ip_switch_streak),
              ],
              [text('Highest streak', 'Langste reeks'), String(downloads.max_ip_switch_streak)],
              [
                text('Last download', 'Laatste download'),
                formatDate(downloads.last_download_at, language),
              ],
            ].map(([label, value]) => (
              <div
                key={label}
                className="rounded-lg border border-(--border-muted) bg-(--bg-soft) p-3"
              >
                <p className="text-[11px] tracking-wide uppercase opacity-50">{label}</p>
                <p className="mt-1 truncate text-sm font-medium" title={value}>
                  {value}
                </p>
              </div>
            ))}
          </div>

          {downloads.recent_downloads.length === 0 ? (
            <EmptyState>
              {text(
                'No file downloads have been recorded for this user yet.',
                'Er zijn nog geen bestandsdownloads voor deze gebruiker vastgelegd.',
              )}
            </EmptyState>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-(--border-muted)">
              <table className="min-w-full text-left text-xs">
                <thead className="bg-(--bg-soft)">
                  <tr>
                    <th className="px-3 py-2 font-medium">{text('Time', 'Tijd')}</th>
                    <th className="px-3 py-2 font-medium">{text('File', 'Bestand')}</th>
                    <th className="px-3 py-2 font-medium">{text('Format', 'Formaat')}</th>
                    <th className="px-3 py-2 font-medium">{text('IP address', 'IP-adres')}</th>
                    <th className="px-3 py-2 font-medium">{text('Client', 'Client')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-(--border-muted)">
                  {downloads.recent_downloads.map((item) => (
                    <tr key={item.id}>
                      <td className="px-3 py-2 whitespace-nowrap">
                        {formatDate(item.created_at, language)}
                      </td>
                      <td className="px-3 py-2">{item.file_name}</td>
                      <td className="px-3 py-2">{item.file_format || '-'}</td>
                      <td className="px-3 py-2 font-mono whitespace-nowrap">{item.source_ip}</td>
                      <td className="max-w-72 truncate px-3 py-2" title={item.user_agent || ''}>
                        {item.user_agent || '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {tab === 'ips' &&
        (ipAddresses.length === 0 ? (
          <EmptyState>
            {text(
              'No IP addresses have been recorded for this user yet.',
              'Er zijn nog geen IP-adressen voor deze gebruiker vastgelegd.',
            )}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-(--border-muted)">
            <table className="min-w-full text-left text-xs">
              <thead className="bg-(--bg-soft)">
                <tr>
                  <th className="px-3 py-2 font-medium">{text('IP address', 'IP-adres')}</th>
                  <th className="px-3 py-2 font-medium">{text('Last seen', 'Laatst gezien')}</th>
                  <th className="px-3 py-2 font-medium">{text('Events', 'Gebeurtenissen')}</th>
                  <th className="px-3 py-2 font-medium">{text('Seen via', 'Gezien via')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-(--border-muted)">
                {ipAddresses.map((item) => (
                  <tr key={item.source_ip}>
                    <td className="px-3 py-2 font-mono whitespace-nowrap">{item.source_ip}</td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {formatDate(item.last_seen_at, language)}
                    </td>
                    <td className="px-3 py-2">{item.event_count}</td>
                    <td className="px-3 py-2">{item.sources.join(', ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}

      {tab === 'kobo' && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h4 className="font-medium">{text('Kobo sync activity', 'Kobo-syncactiviteit')}</h4>
              <p className="mt-1 text-xs opacity-60">
                {text(
                  'One active credential per account. A warning starts after three consecutive IP switches, with no more than 48 hours between switches.',
                  'Per account is één actief toegangsmiddel toegestaan. Na drie opeenvolgende IP-wisselingen binnen telkens 48 uur verschijnt een waarschuwing.',
                )}
              </p>
            </div>
            <span
              className={`rounded-full px-2.5 py-1 text-xs font-medium ${
                kobo.suspicious
                  ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300'
                  : 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
              }`}
            >
              {kobo.suspicious
                ? text('Suspicious activity', 'Verdachte activiteit')
                : text('No Kobo warning', 'Geen Kobo-waarschuwing')}
            </span>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              [
                text('Active token', 'Actief token'),
                kobo.token && !kobo.token.revoked_at ? text('Yes', 'Ja') : text('No', 'Nee'),
              ],
              [text('Recent syncs', 'Recente synchronisaties'), String(kobo.sync_count)],
              [text('IP switch streak', 'Reeks IP-wisselingen'), String(kobo.ip_switch_streak)],
              [
                text('Last sync', 'Laatste synchronisatie'),
                formatDate(kobo.last_sync_at, language),
              ],
            ].map(([label, value]) => (
              <div
                key={label}
                className="rounded-lg border border-(--border-muted) bg-(--bg-soft) p-3"
              >
                <p className="text-[11px] tracking-wide uppercase opacity-50">{label}</p>
                <p className="mt-1 truncate text-sm font-medium" title={value}>
                  {value}
                </p>
              </div>
            ))}
          </div>

          {kobo.recent_syncs.length === 0 ? (
            <EmptyState>
              {text(
                'No Kobo library synchronizations have been recorded yet.',
                'Er zijn nog geen Kobo-bibliotheeksynchronisaties vastgelegd.',
              )}
            </EmptyState>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-(--border-muted)">
              <table className="min-w-full text-left text-xs">
                <thead className="bg-(--bg-soft)">
                  <tr>
                    <th className="px-3 py-2 font-medium">{text('Time', 'Tijd')}</th>
                    <th className="px-3 py-2 font-medium">{text('IP address', 'IP-adres')}</th>
                    <th className="px-3 py-2 font-medium">{text('Changes', 'Wijzigingen')}</th>
                    <th className="px-3 py-2 font-medium">{text('Duration', 'Duur')}</th>
                    <th className="px-3 py-2 font-medium">{text('Status', 'Status')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-(--border-muted)">
                  {kobo.recent_syncs.map((sync) => (
                    <tr key={sync.id}>
                      <td className="px-3 py-2 whitespace-nowrap">
                        {formatDate(sync.created_at, language)}
                      </td>
                      <td className="px-3 py-2 font-mono whitespace-nowrap">{sync.source_ip}</td>
                      <td className="px-3 py-2">{sync.changes_sent}</td>
                      <td className="px-3 py-2">{sync.duration_ms} ms</td>
                      <td className="px-3 py-2">
                        <span
                          className={sync.status === 'failed' ? 'text-red-600' : 'text-emerald-600'}
                        >
                          {sync.error_code || sync.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
};
