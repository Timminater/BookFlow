import { useCallback, useState } from 'react';
import { Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';

import { ConfigSetupBanner } from './components/ConfigSetupBanner';
import { Footer } from './components/Footer';
import { Header } from './components/Header';
import { OnboardingModal } from './components/OnboardingModal';
import { PwaControls } from './components/PwaControls';
import { ToastContainer } from './components/ToastContainer';
import { useLanguage } from './contexts/LanguageContext';
import { useShowOnboardingDebug } from './hooks/app/useShowOnboardingDebug';
import { useStatusChangeNotifications } from './hooks/app/useStatusChangeNotifications';
import { useAuth } from './hooks/useAuth';
import { useDownloadTracking } from './hooks/useDownloadTracking';
import { useDependencyEffect } from './hooks/useMountEffect';
import { useRealtimeStatus } from './hooks/useRealtimeStatus';
import { primeSettingsCache } from './hooks/useSettings';
import { useToast } from './hooks/useToast';
import { primeUsersCache } from './hooks/useUsersFetch';
import { LibraryNavigation } from './library/LibraryNavigation';
import { LibraryPage } from './library/LibraryPage';
import { readOfflinePwaBootstrap, saveOfflinePwaBootstrap } from './library/offline';
import { OfflineReadingCoordinator } from './library/offline/OfflineReadingCoordinator';
import { AdminDownloadsPage } from './pages/AdminDownloadsPage';
import { AdminSettingsPage } from './pages/AdminSettingsPage';
import { AdminUserDetailPage } from './pages/AdminUserDetailPage';
import { AdminUsersPage } from './pages/AdminUsersPage';
import { CatalogBookPage } from './pages/CatalogBookPage';
import { CatalogEntitiesPage } from './pages/CatalogEntitiesPage';
import { CatalogPage } from './pages/CatalogPage';
import { DashboardPage } from './pages/DashboardPage';
import { LoginPage } from './pages/LoginPage';
import { MonitorsPage } from './pages/MonitorsPage';
import { ReleaseProfilesPage } from './pages/ReleaseProfilesPage';
import { RequestsPage } from './pages/RequestsPage';
import { SelfSettingsPage } from './pages/SelfSettingsPage';
import { SuggestionsPage } from './pages/SuggestionsPage';
import { ReaderPage } from './reader/ReaderPage';
import { cancelDownload, getConfig, getSelfSettings, retryDownload } from './services/api';
import { v1Api } from './services/v1Api';
import type { AppConfig } from './types';
import { buildLoginRedirectPath, getReturnToFromSearch } from './utils/authRedirect';
import { catalogBookPath } from './utils/catalogBookPath';

// oxlint-disable-next-line import/no-unassigned-import
import './styles.css';

/** Preserve old links while making the catalog ISBN detail the single book UI. */
const LegacyLibraryBookRedirect = () => {
  const { bookId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { t } = useLanguage();
  useDependencyEffect(() => {
    const id = Number(bookId);
    if (!Number.isInteger(id) || id < 1) {
      void navigate('/library', { replace: true });
      return;
    }
    void v1Api
      .catalogBook(id)
      .then((detail) =>
        navigate(catalogBookPath(detail.book), {
          replace: true,
          state: location.state as unknown,
        }),
      )
      .catch(() => navigate('/library', { replace: true }));
  }, [bookId, location.state, navigate]);
  return <p className="py-10 text-sm opacity-65">{t('common.loading')}</p>;
};

function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const { toasts, showToast, removeToast } = useToast();
  const { setLanguage, t } = useLanguage();
  const {
    isAuthenticated,
    authRequired,
    authChecked,
    isAdmin,
    libraryCapability,
    authMode,
    username,
    displayName,
    oidcButtonLabel,
    hideLocalAuth,
    oidcAutoRedirect,
    loginError,
    isLoggingIn,
    authenticatedServicesEnabled,
    refreshAuth,
    handleLogin,
    handleLogout,
  } = useAuth({ showToast });
  const { status: currentStatus, forceRefresh: fetchStatus } = useRealtimeStatus({
    enabled: authenticatedServicesEnabled,
    pollInterval: 5000,
  });
  const { bookToReleaseMap, markBookCompleted } = useDownloadTracking(currentStatus);

  const [config, setConfig] = useState<AppConfig | null>(null);
  const [libraryNavigationOpen, setLibraryNavigationOpen] = useState(false);
  const [koboEnabled, setKoboEnabled] = useState(false);
  const [configBannerOpen, setConfigBannerOpen] = useState(false);
  const [onboardingOpen, setOnboardingOpen] = useState(false);

  useShowOnboardingDebug({ setOnboardingOpen });

  const loadConfig = useCallback(async () => {
    try {
      const [nextConfig, personalSettings] = await Promise.all([
        getConfig(),
        getSelfSettings().catch(() => null),
      ]);
      setConfig(nextConfig);
      setLanguage(personalSettings?.preferred_language ?? nextConfig.default_ui_language ?? 'en');
      if (nextConfig.settings_enabled && !nextConfig.onboarding_complete) {
        setOnboardingOpen(true);
      }
      const accountId = username ?? (authRequired ? null : 'local');
      if (accountId) {
        await saveOfflinePwaBootstrap({
          profile: {
            accountId,
            username,
            displayName,
            isAdmin,
            libraryCapability,
          },
          config: nextConfig,
        });
      }
    } catch (error) {
      console.error('Failed to load config:', error);
      const cached = await readOfflinePwaBootstrap().catch(() => null);
      if (cached) setConfig(cached.config);
    }
  }, [authRequired, displayName, isAdmin, libraryCapability, setLanguage, username]);

  useDependencyEffect(() => {
    if (!isAuthenticated) return;
    void fetchStatus();
    void loadConfig();
  }, [fetchStatus, isAuthenticated, loadConfig]);

  useDependencyEffect(() => {
    if (!isAuthenticated) {
      setKoboEnabled(false);
      return;
    }
    void v1Api
      .koboSettings()
      .then((settings) => setKoboEnabled(settings.enabled))
      .catch(() => setKoboEnabled(false));
  }, [isAuthenticated]);

  useStatusChangeNotifications({
    currentStatus,
    showToast,
    bookToReleaseMap,
    markBookCompleted,
  });

  const handleSettingsClick = useCallback(() => {
    if (!config?.settings_enabled) {
      setConfigBannerOpen(true);
    } else if (isAdmin) {
      void primeUsersCache();
      void primeSettingsCache();
      void navigate('/admin/settings');
    } else {
      void navigate('/settings');
    }
  }, [config?.settings_enabled, isAdmin, navigate]);

  if (!authChecked || (isAuthenticated && !config)) {
    return (
      <div className="sr-only" aria-live="polite">
        {t('common.loading')}
      </div>
    );
  }

  const app = (
    <>
      <OfflineReadingCoordinator accountId={username ?? 'local'} />
      <div className="fixed top-0 right-0 left-0 z-40">
        <Header
          debug={config?.debug}
          isAdmin={isAdmin}
          authRequired={authRequired}
          isAuthenticated={isAuthenticated}
          issueReportingEnabled={config?.issue_reporting_enabled === true}
          username={username}
          displayName={displayName}
          onLogout={() => {
            void handleLogout();
          }}
          onShowToast={showToast}
          onRemoveToast={removeToast}
        />
      </div>
      <button
        type="button"
        className="fixed top-4 left-4 z-45 rounded-full bg-(--bg) p-2 shadow-sm lg:hidden"
        onClick={() => setLibraryNavigationOpen(true)}
        aria-label={t('nav.open')}
      >
        <svg
          className="h-5 w-5"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.75"
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>
      <LibraryNavigation
        isOpen={libraryNavigationOpen}
        isAdmin={isAdmin}
        koboEnabled={koboEnabled}
        onClose={() => setLibraryNavigationOpen(false)}
      />
      <div className="library-app-shell flex flex-1 flex-col" style={{ paddingTop: '80px' }}>
        <main
          className={`relative mx-auto w-full px-4 py-3 sm:px-6 sm:py-6 ${
            location.pathname === '/' ? 'max-w-none lg:px-4 xl:px-6' : 'max-w-7xl lg:px-8'
          }`}
        >
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/library" element={<LibraryPage koboEnabled={koboEnabled} />} />
            <Route
              path="/library/:bookId/read/:fileId"
              element={<ReaderPage accountId={username ?? 'local'} />}
            />
            <Route path="/kobo" element={<LibraryPage koboEnabled={koboEnabled} koboOnly />} />
            <Route path="/catalog" element={<CatalogPage />} />
            <Route path="/catalog/authors" element={<CatalogEntitiesPage kind="authors" />} />
            <Route path="/catalog/series" element={<CatalogEntitiesPage kind="series" />} />
            <Route
              path="/catalog/:bookId"
              element={<CatalogBookPage canAddToLibrary isAdmin={isAdmin} />}
            />
            <Route
              path="/catalog/isbn/:isbn"
              element={<CatalogBookPage canAddToLibrary isAdmin={isAdmin} />}
            />
            <Route path="/suggestions" element={<Navigate to="/suggestions/overall" replace />} />
            <Route path="/suggestions/:listKey" element={<SuggestionsPage />} />
            <Route
              path="/monitors"
              element={
                <MonitorsPage
                  canAutoAcquire={isAdmin || libraryCapability === 'download-capable'}
                  koboEnabled={koboEnabled}
                />
              }
            />
            <Route
              path="/settings"
              element={
                <SelfSettingsPage
                  onShowToast={showToast}
                  onSettingsSaved={() => void loadConfig()}
                  onKoboEnabledChange={setKoboEnabled}
                />
              }
            />
            <Route
              path="/requests"
              element={<RequestsPage isAdmin={isAdmin} onShowToast={showToast} />}
            />
            {isAdmin && (
              <Route
                path="/admin/request-processes"
                element={<Navigate to="/requests" replace />}
              />
            )}
            {isAdmin && (
              <Route
                path="/admin/downloads"
                element={
                  <AdminDownloadsPage
                    status={currentStatus}
                    onCancel={(id) => void cancelDownload(id).then(fetchStatus)}
                    onRetry={(id) => void retryDownload(id).then(fetchStatus)}
                    onRefresh={() => void fetchStatus()}
                  />
                }
              />
            )}
            {isAdmin && (
              <Route
                path="/admin/settings/profiles"
                element={<ReleaseProfilesPage onShowToast={showToast} />}
              />
            )}
            {isAdmin && (
              <Route
                path="/admin/users"
                element={
                  <AdminUsersPage
                    authMode={authMode}
                    onShowToast={showToast}
                    onRefreshAuth={refreshAuth}
                  />
                }
              />
            )}
            {isAdmin && <Route path="/admin/users/:userId" element={<AdminUserDetailPage />} />}
            {isAdmin && (
              <Route path="/admin/users/:userId/:section" element={<AdminUserDetailPage />} />
            )}
            {isAdmin && (
              <Route
                path="/admin/operations"
                element={<Navigate to="/admin/settings?tab=system-operations" replace />}
              />
            )}
            {isAdmin && (
              <Route
                path="/admin/settings"
                element={
                  <AdminSettingsPage
                    authMode={authMode}
                    onShowToast={showToast}
                    onSettingsSaved={() => void loadConfig()}
                    onRefreshAuth={refreshAuth}
                  />
                }
              />
            )}
            <Route path="/library/:bookId" element={<LegacyLibraryBookRedirect />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
        <Footer
          buildVersion={config?.build_version}
          releaseVersion={config?.release_version}
          debug={config?.debug}
        />
      </div>
      {config && <ConfigSetupBanner settingsEnabled={config.settings_enabled} />}
      <ConfigSetupBanner
        isOpen={configBannerOpen}
        onClose={() => setConfigBannerOpen(false)}
        onContinue={() => {
          setConfigBannerOpen(false);
          handleSettingsClick();
        }}
      />
      <OnboardingModal
        isOpen={onboardingOpen}
        onClose={() => setOnboardingOpen(false)}
        onComplete={() => void loadConfig()}
        onShowToast={showToast}
      />
      <ToastContainer toasts={toasts} />
      <PwaControls />
    </>
  );

  const postLoginPath = getReturnToFromSearch(location.search);
  return (
    <Routes>
      <Route
        path="/login"
        element={
          !authRequired || isAuthenticated ? (
            <Navigate to={postLoginPath} replace />
          ) : (
            <LoginPage
              onLogin={(credentials) => {
                void handleLogin(credentials);
              }}
              error={loginError}
              isLoading={isLoggingIn}
              authMode={authMode}
              oidcButtonLabel={oidcButtonLabel}
              hideLocalAuth={hideLocalAuth}
              oidcAutoRedirect={oidcAutoRedirect}
            />
          )
        }
      />
      <Route
        path="/*"
        element={
          authRequired && !isAuthenticated ? (
            <Navigate to={buildLoginRedirectPath(location)} replace />
          ) : (
            app
          )
        }
      />
    </Routes>
  );
}

export { App };
