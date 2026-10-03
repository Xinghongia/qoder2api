'use client';

import * as React from 'react';

import {ApiError, api, getPanelToken, initApiKey, onUnauthorized, setPanelToken} from '@/lib/api';
import {notify} from '@/lib/toast';

/**
 * 面板会话：与旧看板同一套语义。
 *
 * 挂载时先取 `/panel/status`（免鉴权）问「要不要登录 / 现在算不算已登录」，
 * 未登录就弹对话框；登录成功后 token 只走 `X-Panel-Token` 头，存 sessionStorage。
 */

interface AuthState {
  /** 已经问过 /panel/status（问之前不要渲染业务请求，否则必然 401） */
  ready: boolean;
  /** 面板密码是否还是默认的 admin（顶栏提示用） */
  passwordIsDefault: boolean;
  needsLogin: boolean;
  /**
   * 面板会话版本号：登录/登出各 +1。组件把它放进 effect 依赖，
   * 就能在「登录成功」后自动重拉数据（而不是停在首屏那次 401 的空态）。
   */
  sessionEpoch: number;
  login: (password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshStatus: () => Promise<void>;
}

const AuthContext = React.createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return ctx;
}

export function AuthProvider({children}: {children: React.ReactNode}) {
  const [ready, setReady] = React.useState(false);
  const [needsLogin, setNeedsLogin] = React.useState(false);
  const [passwordIsDefault, setPasswordIsDefault] = React.useState(false);
  const [sessionEpoch, setSessionEpoch] = React.useState(0);

  const refreshStatus = React.useCallback(async () => {
    try {
      const status = await api.panel.status();
      setNeedsLogin(!status.authenticated);
      setPasswordIsDefault(!!status.panel_password_is_default);
    } catch {
      // 网关没起来时不要把用户锁在登录框里，保持可用并让页面自己报错
      setNeedsLogin(false);
    } finally {
      setReady(true);
    }
  }, []);

  React.useEffect(() => {
    initApiKey();
    void refreshStatus();
  }, [refreshStatus]);

  // 任何一个请求撞上 401/403 都回到登录框（与旧看板一致，不整页跳转）
  React.useEffect(() => onUnauthorized(() => setNeedsLogin(true)), []);

  const login = React.useCallback(async (password: string) => {
    const r = await api.panel.login(password);
    setPanelToken(r.token || '');
    setNeedsLogin(false);
    setSessionEpoch((n) => n + 1);   // 让各面板就地重拉（不刷新整页）
    await refreshStatus();
  }, [refreshStatus]);

  const logout = React.useCallback(async () => {
    try {
      await api.panel.logout();
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
    }
    setPanelToken('');
    setNeedsLogin(true);
    setSessionEpoch((n) => n + 1);
    notify.info('已退出面板');
  }, []);

  const value = React.useMemo<AuthState>(
    () => ({ready, needsLogin, passwordIsDefault, sessionEpoch, login, logout, refreshStatus}),
    [ready, needsLogin, passwordIsDefault, sessionEpoch, login, logout, refreshStatus],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** 面板登录对话框（未登录时由布局自动弹出，也可手动打开）。 */
export function PanelLoginDialog() {
  const {needsLogin, login, ready} = useAuth();
  const [password, setPassword] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await login(password);
      setPassword('');
      notify.ok('已登录面板');
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败');
    } finally {
      setBusy(false);
    }
  };

  if (!ready || !needsLogin) return null;

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/16 backdrop-blur-[2px] dark:bg-black/55">
      <form
        onSubmit={submit}
        className="w-[calc(100%-2rem)] max-w-sm rounded-[24px] bg-muted p-5 shadow-[0_24px_60px_rgba(15,23,42,0.10)]"
      >
        <h2 className="text-base font-semibold tracking-[-0.01em]">面板登录</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          管理接口需要面板密码（默认 admin，登录后请尽快修改）。
        </p>
        <input
          type="password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="面板密码"
          className="mt-4 h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        {error && <p className="mt-2 text-xs text-red-500">{error}</p>}
        <button
          type="submit"
          disabled={busy || !password}
          className="mt-4 h-9 w-full rounded-md bg-primary text-sm font-medium text-primary-foreground transition-opacity disabled:opacity-50"
        >
          {busy ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  );
}
