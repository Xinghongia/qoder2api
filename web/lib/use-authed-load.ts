'use client';

import * as React from 'react';

import {useAuth} from '@/lib/auth-context';

/**
 * 统一的「什么时候可以发业务请求」时机。
 *
 * 为什么需要它：面板 token 存在 sessionStorage，首屏 `/panel/status` 没回来之前
 * 发请求必然 401；而登录成功后如果不再触发一次，页面会一直停在空态
 * （用户只能手动点刷新）。这个 hook 同时解决两件事：
 *   1. 未就绪 / 需要登录时**不发**请求；
 *   2. 登录成功（sessionEpoch +1）后就地重拉，不刷新整页。
 */
export function useAuthedLoad(load: () => void, deps: React.DependencyList = []) {
  const {ready, needsLogin, sessionEpoch} = useAuth();
  React.useEffect(() => {
    if (!ready || needsLogin) return;
    void load();
    // load 由调用方 useCallback 稳定；deps 由调用方显式声明
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, needsLogin, sessionEpoch, ...deps]);
}

/** 便捷判断：现在是否处于「可以发请求」的状态。 */
export function useAuthed(): boolean {
  const {ready, needsLogin} = useAuth();
  return ready && !needsLogin;
}
