'use client';

import * as React from 'react';

import {ApiError, api, getPanelToken, initApiKey, onUnauthorized, setPanelToken} from '@/lib/api';
import {notify} from '@/lib/toast';

/**
 * 面板会话：与旧看板同一套语义。
 *
 * 挂载时先取 `/panel/status`（免鉴权）问「要不要登录 / 现在算不算已登录」，
 * 未登录由 `(main)` 布局重定向到 `/login`；登录成功后 token 只走
 * `X-Panel-Token` 头，存 sessionStorage。
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
  login: (username: string, password: string) => Promise<void>;
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

  // 请求撞上 401（会话无效）才回到登录框（与旧看板一致，不整页跳转）；
  // 403 是服务端有意的拒绝（如「默认密码不交明文 Key」），会话是好的，
  // 由调用方把服务端说明原样提示，不能弹登录框（上游 v1.2.9）。
  React.useEffect(() => onUnauthorized(() => setNeedsLogin(true)), []);

  const login = React.useCallback(async (username: string, password: string) => {
    const r = await api.panel.login(username, password);
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
