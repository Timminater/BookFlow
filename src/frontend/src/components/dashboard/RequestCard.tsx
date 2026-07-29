import { Link } from 'react-router-dom';

import { useLanguage } from '../../contexts/LanguageContext';
import type { TranslationKey } from '../../i18n/translations';
import { requestStatusKind, requestStatusTone } from '../../pages/dashboardData';
import type { V1UserRequest } from '../../types/v1';
import { withBasePath } from '../../utils/basePath';

const STATUS_KEYS: Record<ReturnType<typeof requestStatusKind>, TranslationKey> = {
  available: 'dashboard.requestStatus.available',
  processing: 'dashboard.requestStatus.processing',
  rejected: 'dashboard.requestStatus.rejected',
  cancelled: 'dashboard.requestStatus.cancelled',
  failed: 'dashboard.requestStatus.failed',
  unknown: 'dashboard.requestStatus.unknown',
};

const STATUS_CLASSES: Record<ReturnType<typeof requestStatusTone>, string> = {
  success: 'bg-emerald-500/90 text-white',
  progress: 'bg-violet-500/90 text-white',
  danger: 'bg-red-500/90 text-white',
  neutral: 'bg-slate-600/90 text-white',
};

export const RequestCard = ({ request }: { request: V1UserRequest }) => {
  const { language, t } = useLanguage();
  const tone = requestStatusTone(request.status, request.delivery_state);
  const kind = requestStatusKind(request.status, request.delivery_state);
  const formattedDate = new Date(request.created_at).toLocaleDateString(
    language === 'nl' ? 'nl-NL' : 'en-GB',
    { day: 'numeric', month: 'short', year: 'numeric' },
  );

  return (
    <Link
      to={`/catalog/${request.book_id}`}
      aria-label={`${request.title}: ${t(STATUS_KEYS[kind])}`}
      className="group relative min-h-36 w-full max-w-[19rem] shrink-0 snap-start overflow-hidden rounded-xl border border-(--border-muted) bg-(--bg-soft) shadow-lg transition duration-200 hover:-translate-y-0.5 hover:border-violet-500/60 focus-visible:ring-2 focus-visible:ring-violet-500 sm:max-w-[23rem]"
    >
      {request.cover_url && (
        <img
          src={withBasePath(request.cover_url)}
          alt=""
          className="absolute inset-0 h-full w-full scale-110 object-cover opacity-30 blur-[1px] transition duration-300 group-hover:scale-115"
        />
      )}
      <div className="absolute inset-0 bg-linear-to-r from-slate-950 via-slate-950/90 to-slate-950/35" />
      <div className="relative flex min-h-36">
        <div className="flex min-w-0 flex-1 flex-col p-3 text-white sm:p-4">
          <p className="truncate text-[10px] font-semibold tracking-widest text-slate-400 uppercase">
            {t('dashboard.requestBook')} · {formattedDate}
          </p>
          <h3 className="mt-1 line-clamp-2 text-base font-semibold wrap-break-word">
            {request.title}
          </h3>
          <p className="mt-1 truncate text-xs text-slate-400">
            {request.author || t('common.unknownAuthor')}
          </p>
          <span
            className={`mt-auto inline-flex max-w-full self-start truncate rounded-full px-2 py-1 text-[10px] font-semibold ${STATUS_CLASSES[tone]}`}
          >
            {t(STATUS_KEYS[kind])}
          </span>
        </div>
        <div className="m-3 ml-0 aspect-[2/3] w-16 shrink-0 self-center overflow-hidden rounded-lg border border-white/10 bg-slate-800 sm:w-20">
          {request.cover_url ? (
            <img
              src={withBasePath(request.cover_url)}
              alt=""
              loading="lazy"
              className="h-full w-full object-cover"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center px-2 text-center text-xs font-semibold text-white">
              <span className="line-clamp-4 wrap-break-word">{request.title}</span>
            </div>
          )}
        </div>
      </div>
    </Link>
  );
};
