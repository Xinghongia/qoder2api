'use client';

import * as React from 'react';

import {api} from '@/lib/api';

/**
 * 区域出口状态：`mode`（intl / cn / both）由后端持久化在
 * accounts/active_realm.json；`view` 是当前页面正在看的账号区（网关页的
 * 国际版 / 国内版子标签），不写回后端。
 */

export type Realm = 'intl' | 'cn';
export type RealmMode = 'intl' | 'cn' | 'both';

interface RealmState {
  ready: boolean;
  mode: RealmMode;
  preferred: Realm;
  view: Realm;
  setMode: (mode: RealmMode, preferred?: Realm) => Promise<void>;
  setView: (realm: Realm) => void;
}

const RealmContext = React.createContext<RealmState | null>(null);

export function useRealm(): RealmState {
  const ctx = React.useContext(RealmContext);
  if (!ctx) throw new Error('useRealm 必须在 RealmProvider 内使用');
  return ctx;
}

export function RealmProvider({children}: {children: React.ReactNode}) {
  const [ready, setReady] = React.useState(false);
  const [mode, setModeState] = React.useState<RealmMode>('cn');
  const [preferred, setPreferred] = React.useState<Realm>('cn');
  const [view, setView] = React.useState<Realm>('cn');

  React.useEffect(() => {
    let alive = true;
    api.realm
      .get()
      .then((r) => {
        if (!alive) return;
        const m = (r.mode as RealmMode) || 'cn';
        setModeState(m);
        setPreferred((r.preferred as Realm) || (m === 'both' ? 'cn' : m));
        setView((r.preferred as Realm) || (m === 'both' ? 'cn' : m));
      })
      .catch(() => undefined)
      .finally(() => alive && setReady(true));
    return () => {
      alive = false;
    };
  }, []);

  const setMode = React.useCallback(async (next: RealmMode, pref?: Realm) => {
    await api.realm.set(next, pref);
    setModeState(next);
    const p = pref || (next === 'both' ? 'cn' : (next as Realm));
    setPreferred(p);
    setView(p);
  }, []);

  const value = React.useMemo<RealmState>(
    () => ({ready, mode, preferred, view, setMode, setView}),
    [ready, mode, preferred, view, setMode],
  );

  return <RealmContext.Provider value={value}>{children}</RealmContext.Provider>;
}
