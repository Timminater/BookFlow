import type { ReactNode } from 'react';

import { useLanguage } from '../../../contexts/LanguageContext';
import type { AdminUser } from '../../../services/api';
import type {
  PasswordFieldConfig,
  SelectFieldConfig,
  SelectOption,
  TextFieldConfig,
} from '../../../types/settings';
import { DropdownList } from '../../DropdownList';
import { Tooltip } from '../../shared/Tooltip';
import { CheckboxField, PasswordField, SelectField, TextField } from '../fields';
import { FieldWrapper } from '../shared';
import type { CreateUserFormState } from './types';
import { UserAuthSourceBadge } from './UserAuthSourceBadge';

const UserCardShell = ({ title, children }: { title: string; children: ReactNode }) => (
  <div className="space-y-5 rounded-lg border border-(--border-muted) bg-(--bg) p-4">
    <h3 className="text-sm font-medium">{title}</h3>
    {children}
  </div>
);

const CREATE_ROLE_OPTIONS: SelectOption[] = [
  { value: 'user', label: 'User' },
  { value: 'admin', label: 'Admin' },
];

const EDIT_ROLE_OPTIONS: SelectOption[] = [
  { value: 'admin', label: 'Admin' },
  { value: 'user', label: 'User' },
];

const LIBRARY_CAPABILITY_OPTIONS: SelectOption[] = [
  { value: 'download-capable', label: 'Download capable' },
  { value: 'request-only', label: 'Request only' },
];

const createTextField = (
  key: string,
  label: string,
  value: string,
  placeholder: string,
  required = false,
): TextFieldConfig => ({
  type: 'TextField',
  key,
  label,
  value,
  placeholder,
  required,
});

const createPasswordField = (
  key: string,
  label: string,
  value: string,
  placeholder: string,
  required = false,
): PasswordFieldConfig => ({
  type: 'PasswordField',
  key,
  label,
  value,
  placeholder,
  required,
});

const createRoleField = (value: string, options: SelectOption[]): SelectFieldConfig => ({
  type: 'SelectField',
  key: 'role',
  label: 'Role',
  value,
  options,
});

const renderTextField = (
  field: TextFieldConfig,
  value: string,
  onChange: (value: string) => void,
  disabled = false,
  disabledReason?: string,
) => (
  <FieldWrapper field={field} disabledOverride={disabled} disabledReasonOverride={disabledReason}>
    <TextField field={field} value={value} onChange={onChange} disabled={disabled} />
  </FieldWrapper>
);

const renderSelectField = (
  field: SelectFieldConfig,
  value: string,
  onChange: (value: string) => void,
  disabled = false,
  disabledReason?: string,
) => (
  <FieldWrapper field={field} disabledOverride={disabled} disabledReasonOverride={disabledReason}>
    <SelectField field={field} value={value} onChange={onChange} disabled={disabled} />
  </FieldWrapper>
);

const renderPasswordField = (
  field: PasswordFieldConfig,
  value: string,
  onChange: (value: string) => void,
) => (
  <FieldWrapper field={field}>
    <PasswordField field={field} value={value} onChange={onChange} />
  </FieldWrapper>
);

const getRoleBadgeClassName = (role: string, disabled = false) =>
  `inline-flex items-center rounded-md px-2.5 py-1 text-xs font-medium leading-none ${
    disabled ? 'cursor-not-allowed' : ''
  } ${role === 'admin' ? 'bg-sky-500/15 text-sky-600 dark:text-sky-400' : 'bg-zinc-500/10 opacity-70'}`;

interface UserRoleControlProps {
  user: AdminUser;
  onUserChange?: (user: AdminUser) => void;
  oidcAdminGroup?: string;
  tooltipPosition?: 'top' | 'bottom' | 'left' | 'right';
}

export const UserRoleControl = ({
  user,
  onUserChange,
  oidcAdminGroup,
  tooltipPosition = 'bottom',
}: UserRoleControlProps) => {
  const { t } = useLanguage();
  const roleLabel = user.role === 'admin' ? t('admin.roleAdmin') : t('admin.roleUser');
  const canEditRole = Boolean(onUserChange) && user.edit_capabilities.canEditRole;
  let roleDisabledReason: string | undefined;
  if (!user.edit_capabilities.canEditRole) {
    if (user.edit_capabilities.authSource === 'oidc') {
      roleDisabledReason = oidcAdminGroup
        ? t('admin.roleManagedByGroup', { group: oidcAdminGroup })
        : t('admin.roleManagedByOidc');
    } else if (user.edit_capabilities.authSource === 'builtin') {
      roleDisabledReason = t('admin.roleAdminsOnly');
    } else {
      roleDisabledReason = t('admin.roleManagedExternally');
    }
  }

  if (canEditRole && onUserChange) {
    return (
      <DropdownList
        options={EDIT_ROLE_OPTIONS.map((option) => ({
          value: option.value,
          label: option.value === 'admin' ? t('admin.roleAdmin') : t('admin.roleUser'),
        }))}
        value={user.role}
        onChange={(value) => {
          const nextRole = Array.isArray(value) ? (value[0] ?? '') : value;
          onUserChange({ ...user, role: nextRole });
        }}
        widthClassName="w-28"
        buttonClassName={`py-1! px-2.5! text-xs! font-medium! ${
          user.role === 'admin'
            ? 'bg-sky-500/15! text-sky-600! dark:text-sky-400! border-sky-500/30!'
            : 'bg-zinc-500/10! opacity-70!'
        }`}
      />
    );
  }

  if (onUserChange && !user.edit_capabilities.canEditRole) {
    return (
      <Tooltip
        content={roleDisabledReason || t('admin.roleCannotChange')}
        position={tooltipPosition}
      >
        <span className={getRoleBadgeClassName(user.role, true)}>{roleLabel}</span>
      </Tooltip>
    );
  }

  return <span className={getRoleBadgeClassName(user.role)}>{roleLabel}</span>;
};

interface UserIdentityHeaderProps {
  user: AdminUser;
  showAuthSource?: boolean;
  showInactiveState?: boolean;
}

export const UserIdentityHeader = ({
  user,
  showAuthSource = true,
  showInactiveState = true,
}: UserIdentityHeaderProps) => {
  const { t } = useLanguage();
  const active = user.is_active;

  return (
    <div className="flex min-w-0 flex-1 items-center gap-3">
      <div
        className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-medium ${user.role === 'admin' ? 'bg-sky-500/20 text-sky-600 dark:text-sky-400' : 'bg-zinc-500/20'}`}
      >
        {user.username.charAt(0).toUpperCase()}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-medium">{user.display_name || user.username}</span>
          {user.display_name && (
            <span className="truncate text-xs opacity-40">@{user.username}</span>
          )}
          {showAuthSource && <UserAuthSourceBadge user={user} showInactive={false} />}
        </div>
        <div className="truncate text-xs opacity-50">{user.email || t('admin.noEmail')}</div>
        {showInactiveState && !active && (
          <div className="truncate text-[11px] opacity-60">{t('admin.inactiveAuthMode')}</div>
        )}
      </div>
    </div>
  );
};

interface UserEditActionsProps {
  onSave: () => void;
  saving: boolean;
  saveDisabled?: boolean;
  onCancel: () => void;
  cancelDisabled?: boolean;
  onDelete?: () => void;
  onConfirmDelete?: () => void;
  onCancelDelete?: () => void;
  isDeletePending?: boolean;
  deleting?: boolean;
  variant?: 'card' | 'modalFooter';
}

export const UserEditActions = ({
  onSave,
  saving,
  saveDisabled = false,
  onCancel,
  cancelDisabled = false,
  onDelete,
  onConfirmDelete,
  onCancelDelete,
  isDeletePending = false,
  deleting = false,
  variant = 'card',
}: UserEditActionsProps) => {
  const { t } = useLanguage();
  if (variant === 'modalFooter') {
    return (
      <div className="flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={onCancel}
          disabled={cancelDisabled}
          className="hover-action rounded-lg border border-(--border-muted) bg-(--bg-soft) px-4 py-2 text-sm font-medium shadow-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t('common.cancel')}
        </button>
        <button
          type="button"
          onClick={onSave}
          disabled={saveDisabled}
          className="inline-flex items-center gap-2 rounded-lg bg-sky-600 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {saving ? (
            <>
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24">
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                  fill="none"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                />
              </svg>
              {t('admin.savingChanges')}
            </>
          ) : (
            t('admin.saveChanges')
          )}
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 border-t border-(--border-muted) pt-3 sm:flex-row sm:items-center">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onSave}
          disabled={saveDisabled}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {saving ? t('admin.savingChanges') : t('admin.saveChanges')}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={cancelDisabled}
          className="rounded-lg border border-(--border-muted) bg-(--bg) px-4 py-2 text-sm font-medium transition-colors hover:bg-(--hover-surface) disabled:cursor-not-allowed disabled:opacity-60"
        >
          {t('common.cancel')}
        </button>
      </div>
      {onDelete && (
        <div className="flex flex-wrap gap-2 sm:ml-auto">
          {isDeletePending ? (
            <>
              <button
                type="button"
                onClick={onConfirmDelete}
                disabled={deleting}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {deleting ? t('admin.deletingUser') : t('admin.confirmDeleteUser')}
              </button>
              <button
                type="button"
                onClick={onCancelDelete}
                disabled={deleting}
                className="rounded-lg border border-(--border-muted) bg-(--bg) px-4 py-2 text-sm font-medium transition-colors hover:bg-(--hover-surface) disabled:cursor-not-allowed disabled:opacity-60"
              >
                {t('common.cancel')}
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={onDelete}
              className="rounded-lg border border-red-500/40 px-4 py-2 text-sm font-medium text-red-600 transition-colors hover:bg-red-500/10"
            >
              {t('admin.deleteUser')}
            </button>
          )}
        </div>
      )}
    </div>
  );
};

interface UserCreateCardProps {
  form: CreateUserFormState;
  onChange: (form: CreateUserFormState) => void;
  creating: boolean;
  isFirstUser: boolean;
  needsLocalAdmin?: boolean;
  onSubmit: () => void;
  onCancel: () => void;
}

export const UserCreateCard = ({
  form,
  onChange,
  creating,
  isFirstUser,
  needsLocalAdmin = false,
  onSubmit,
  onCancel,
}: UserCreateCardProps) => {
  const { t } = useLanguage();
  const usernameField = createTextField(
    'username',
    t('settings.username'),
    form.username,
    t('settings.username'),
    true,
  );
  const roleField = createRoleField(
    form.role,
    CREATE_ROLE_OPTIONS.map((option) => ({
      value: option.value,
      label: option.value === 'admin' ? t('admin.roleAdmin') : t('admin.roleUser'),
    })),
  );
  const capabilityField: SelectFieldConfig = {
    type: 'SelectField',
    key: 'library_capability',
    label: t('admin.libraryCapability'),
    value: form.library_capability,
    options: LIBRARY_CAPABILITY_OPTIONS.map((option) => ({
      value: option.value,
      label: option.value === 'request-only' ? t('admin.requestOnly') : t('admin.downloadCapable'),
    })),
  };
  const displayNameField = createTextField(
    'display_name',
    t('settings.displayName'),
    form.display_name,
    t('settings.displayName'),
  );
  const emailField = createTextField('email', t('settings.email'), form.email, 'user@example.com');
  const passwordField = createPasswordField(
    'password',
    t('settings.password'),
    form.password,
    t('admin.minimumFourCharacters'),
    true,
  );
  const confirmPasswordField = createPasswordField(
    'confirm_password',
    t('admin.confirmPassword'),
    form.password_confirm,
    t('admin.confirmPassword'),
    true,
  );

  return (
    <UserCardShell title={t('admin.createLocalUser')}>
      {isFirstUser && <p className="text-xs text-zinc-500">{t('admin.firstAccountHelp')}</p>}
      {needsLocalAdmin && !isFirstUser && (
        <p className="text-xs text-zinc-500">{t('admin.oidcAdminRequired')}</p>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {renderTextField(usernameField, form.username, (value) =>
          onChange({ ...form, username: value }),
        )}
        {renderSelectField(roleField, form.role, (value) => onChange({ ...form, role: value }))}
        {renderSelectField(capabilityField, form.library_capability, (value) =>
          onChange({
            ...form,
            library_capability: value === 'request-only' ? 'request-only' : 'download-capable',
          }),
        )}
        {renderTextField(displayNameField, form.display_name, (value) =>
          onChange({ ...form, display_name: value }),
        )}
        {renderTextField(emailField, form.email, (value) => onChange({ ...form, email: value }))}
        <FieldWrapper
          field={{
            type: 'CheckboxField',
            key: 'auto_approve_requests',
            label: t('admin.autoApproveRequests'),
            value: form.auto_approve_requests,
            description: t('admin.autoApproveRequestsHelp'),
          }}
        >
          <CheckboxField
            field={{
              type: 'CheckboxField',
              key: 'auto_approve_requests',
              label: t('admin.autoApproveRequests'),
              value: form.auto_approve_requests,
              description: t('admin.autoApproveRequestsHelp'),
            }}
            value={form.auto_approve_requests}
            onChange={(value) => onChange({ ...form, auto_approve_requests: value })}
          />
        </FieldWrapper>
      </div>

      {renderPasswordField(passwordField, form.password, (value) =>
        onChange({ ...form, password: value }),
      )}

      {renderPasswordField(confirmPasswordField, form.password_confirm, (value) =>
        onChange({ ...form, password_confirm: value }),
      )}

      <div className="flex items-center gap-2 pt-1">
        <button
          type="button"
          onClick={onSubmit}
          disabled={creating}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {creating ? t('admin.creatingUser') : t('admin.createLocalUser')}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-lg border border-(--border-muted) bg-(--bg) px-4 py-2 text-sm font-medium transition-colors hover:bg-(--hover-surface)"
        >
          {t('common.cancel')}
        </button>
      </div>
    </UserCardShell>
  );
};

interface UserEditFieldsProps {
  user: AdminUser;
  onUserChange: (user: AdminUser) => void;
  onSave: () => void;
  saving: boolean;
  onCancel: () => void;
  hideActions?: boolean;
  editPassword: string;
  onEditPasswordChange: (value: string) => void;
  editPasswordConfirm: string;
  onEditPasswordConfirmChange: (value: string) => void;
  onDelete?: () => void;
  onConfirmDelete?: () => void;
  onCancelDelete?: () => void;
  isDeletePending?: boolean;
  deleting?: boolean;
}

const UserEditFields = ({
  user,
  onUserChange,
  onSave,
  saving,
  onCancel,
  hideActions = false,
  editPassword,
  onEditPasswordChange,
  editPasswordConfirm,
  onEditPasswordConfirmChange,
  onDelete,
  onConfirmDelete,
  onCancelDelete,
  isDeletePending = false,
  deleting = false,
}: UserEditFieldsProps) => {
  const { t } = useLanguage();
  const capabilities = user.edit_capabilities;
  const { authSource, canSetPassword, canEditEmail, canEditDisplayName } = capabilities;
  const usernameField = createTextField(
    'username',
    t('settings.username'),
    user.username,
    t('settings.username'),
    true,
  );

  const displayNameField = createTextField(
    'display_name',
    t('settings.displayName'),
    user.display_name || '',
    t('settings.displayName'),
  );
  const emailField = createTextField(
    'email',
    t('settings.email'),
    user.email || '',
    'user@example.com',
  );
  const capabilityField: SelectFieldConfig = {
    type: 'SelectField',
    key: 'library_capability',
    label: t('admin.libraryCapability'),
    value: user.library_capability,
    options: LIBRARY_CAPABILITY_OPTIONS.map((option) => ({
      value: option.value,
      label: option.value === 'request-only' ? t('admin.requestOnly') : t('admin.downloadCapable'),
    })),
  };
  const activeField = {
    type: 'CheckboxField' as const,
    key: 'is_active',
    label: t('admin.activeAccount'),
    value: user.is_active,
  };
  const newPasswordField = createPasswordField(
    'new_password',
    t('admin.newPassword'),
    editPassword,
    t('admin.keepPasswordHelp'),
  );
  const confirmPasswordField = createPasswordField(
    'confirm_password',
    t('admin.confirmPassword'),
    editPasswordConfirm,
    t('admin.confirmNewPassword'),
    true,
  );

  const displayNameDisabledReason = !canEditDisplayName ? t('admin.displayNameManaged') : undefined;

  let emailDisabledReason: string | undefined;
  if (!canEditEmail) {
    emailDisabledReason =
      authSource === 'cwa' ? t('admin.emailSyncedCwa') : t('admin.emailManaged');
  }

  return (
    <>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {renderTextField(usernameField, user.username, (value) =>
          onUserChange({ ...user, username: value }),
        )}
        {renderTextField(
          displayNameField,
          user.display_name || '',
          (value) => onUserChange({ ...user, display_name: value || null }),
          !canEditDisplayName,
          displayNameDisabledReason,
        )}

        {renderTextField(
          emailField,
          user.email || '',
          (value) => onUserChange({ ...user, email: value || null }),
          !canEditEmail,
          emailDisabledReason,
        )}
        {renderSelectField(capabilityField, user.library_capability, (value) =>
          onUserChange({
            ...user,
            library_capability: value === 'request-only' ? 'request-only' : 'download-capable',
          }),
        )}
        <FieldWrapper field={activeField}>
          <CheckboxField
            field={activeField}
            value={user.is_active}
            onChange={(value) => onUserChange({ ...user, is_active: value })}
          />
        </FieldWrapper>
        <FieldWrapper
          field={{
            type: 'CheckboxField',
            key: 'auto_approve_requests',
            label: t('admin.autoApproveRequests'),
            value: user.auto_approve_requests,
            description: t('admin.autoApproveRequestsHelp'),
          }}
        >
          <CheckboxField
            field={{
              type: 'CheckboxField',
              key: 'auto_approve_requests',
              label: t('admin.autoApproveRequests'),
              value: user.auto_approve_requests,
              description: t('admin.autoApproveRequestsHelp'),
            }}
            value={user.auto_approve_requests}
            onChange={(value) => onUserChange({ ...user, auto_approve_requests: value })}
          />
        </FieldWrapper>
      </div>

      {canSetPassword && (
        <>
          {renderPasswordField(newPasswordField, editPassword, onEditPasswordChange)}

          {editPassword &&
            renderPasswordField(
              confirmPasswordField,
              editPasswordConfirm,
              onEditPasswordConfirmChange,
            )}
        </>
      )}

      {!hideActions && (
        <UserEditActions
          onSave={onSave}
          saving={saving}
          saveDisabled={saving}
          onCancel={onCancel}
          onDelete={onDelete}
          onConfirmDelete={onConfirmDelete}
          onCancelDelete={onCancelDelete}
          isDeletePending={isDeletePending}
          deleting={deleting}
        />
      )}
    </>
  );
};

interface UserPreferencesPanelProps {
  description?: string;
  hideTitle?: boolean;
  actionLabel?: string;
  onAction?: () => void;
  children?: ReactNode;
}

interface UserAccountCardContentProps extends Omit<UserEditFieldsProps, 'hideActions'> {
  hideEditActions?: boolean;
  preferencesPanel?: UserPreferencesPanelProps;
  preferencesPlacement?: 'before' | 'after';
}

const UserPreferencesPanel = ({ panel }: { panel: UserPreferencesPanelProps }) => {
  const { t } = useLanguage();
  return (
    <div className="space-y-3">
      {(!panel.hideTitle || panel.onAction) && (
        <div>
          {!panel.hideTitle && <p className="text-sm font-medium">{t('admin.userPreferences')}</p>}
          {!panel.hideTitle && panel.description && (
            <p className="mt-0.5 text-xs opacity-60">{panel.description}</p>
          )}
          {panel.onAction && (
            <button
              type="button"
              onClick={panel.onAction}
              className="mt-2 rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700"
            >
              {panel.actionLabel || t('admin.openUserPreferences')}
            </button>
          )}
        </div>
      )}
      {panel.children}
    </div>
  );
};

export const UserAccountCardContent = ({
  user,
  onUserChange,
  onSave,
  saving,
  onCancel,
  hideEditActions = false,
  editPassword,
  onEditPasswordChange,
  editPasswordConfirm,
  onEditPasswordConfirmChange,
  onDelete,
  onConfirmDelete,
  onCancelDelete,
  isDeletePending,
  deleting,
  preferencesPanel,
  preferencesPlacement = 'before',
}: UserAccountCardContentProps) => {
  const preferencesContent = preferencesPanel ? (
    <UserPreferencesPanel panel={preferencesPanel} />
  ) : null;

  return (
    <div className="space-y-5">
      {preferencesContent && preferencesPlacement === 'before' && (
        <>
          {preferencesContent}
          <div className="border-t border-(--border-muted)" />
        </>
      )}

      <UserEditFields
        user={user}
        onUserChange={onUserChange}
        onSave={onSave}
        saving={saving}
        onCancel={onCancel}
        hideActions={hideEditActions}
        editPassword={editPassword}
        onEditPasswordChange={onEditPasswordChange}
        editPasswordConfirm={editPasswordConfirm}
        onEditPasswordConfirmChange={onEditPasswordConfirmChange}
        onDelete={onDelete}
        onConfirmDelete={onConfirmDelete}
        onCancelDelete={onCancelDelete}
        isDeletePending={isDeletePending}
        deleting={deleting}
      />

      {preferencesContent && preferencesPlacement === 'after' && (
        <>
          <div className="border-t border-(--border-muted)" />
          {preferencesContent}
        </>
      )}
    </div>
  );
};
