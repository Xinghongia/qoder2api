'use client';

import * as React from 'react';

import {DesktopScanDialog} from '@/components/common/accounts/DesktopScanDialog';
import {ImportAccountsDialog} from '@/components/common/accounts/ImportAccountsDialog';
import {OAuthDeviceDialog} from '@/components/common/accounts/OAuthDeviceDialog';
import {PatImportDialog} from '@/components/common/accounts/PatImportDialog';

/**
 * 「添加账号」全局上下文。
 *
 * 四个导入弹窗挂在主布局里（底部栏的 + 按钮、账号页工具栏都从这里打开），
 * 成功后 `bump()` 递增 refreshKey —— 页面把它放进加载依赖即可**原地刷新**，
 * 不切换视图、不整页重载（旧看板曾因批量动作跳区被投诉）。
 */

export type AddAccountKind = 'oauth' | 'desktop' | 'pat' | 'json';

interface AddAccountContextValue {
  open: (kind: AddAccountKind) => void;
  /** 账号数据变更计数（导入成功 +1）。 */
  refreshKey: number;
  bump: () => void;
}

const AddAccountContext = React.createContext<AddAccountContextValue | null>(null);

export function useAddAccount(): AddAccountContextValue {
  const ctx = React.useContext(AddAccountContext);
  if (!ctx) throw new Error('useAddAccount 必须在 AddAccountProvider 内使用');
  return ctx;
}

export function AddAccountProvider({children}: {children: React.ReactNode}) {
  const [which, setWhich] = React.useState<AddAccountKind | null>(null);
  /**
   * 关闭的弹窗**不进 React 树**：它们会消费 useRealm，切换国际/国内时会被
   * 上下文变更重渲染——常驻挂载时偶发「闪一下」。关闭后延迟 220ms 再卸载，
   * 让 150ms 的退出动画播完；重开时取消卸载。
   */
  const [visible, setVisible] = React.useState<AddAccountKind | null>(null);
  const unmountTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const [refreshKey, setRefreshKey] = React.useState(0);

  const bump = React.useCallback(() => setRefreshKey((n) => n + 1), []);

  const open = React.useCallback((kind: AddAccountKind) => {
    if (unmountTimer.current) {
      clearTimeout(unmountTimer.current);
      unmountTimer.current = null;
    }
    setVisible(kind);
    setWhich(kind);
  }, []);

  const close = React.useCallback(() => {
    setWhich(null);
    if (unmountTimer.current) clearTimeout(unmountTimer.current);
    unmountTimer.current = setTimeout(() => {
      setVisible(null);
      unmountTimer.current = null;
    }, 220);
  }, []);

  React.useEffect(
    () => () => {
      if (unmountTimer.current) clearTimeout(unmountTimer.current);
    },
    [],
  );

  const handleOpenChange = React.useCallback(
    (o: boolean) => {
      if (!o) close();
    },
    [close],
  );

  const value = React.useMemo(
    () => ({open, refreshKey, bump}),
    [open, refreshKey, bump],
  );

  return (
    <AddAccountContext.Provider value={value}>
      {children}
      {visible === 'oauth' && (
        <OAuthDeviceDialog
          open={which === 'oauth'}
          onOpenChange={handleOpenChange}
          onChanged={bump}
        />
      )}
      {visible === 'desktop' && (
        <DesktopScanDialog
          open={which === 'desktop'}
          onOpenChange={handleOpenChange}
          onChanged={bump}
        />
      )}
      {visible === 'pat' && (
        <PatImportDialog
          open={which === 'pat'}
          onOpenChange={handleOpenChange}
          onChanged={bump}
        />
      )}
      {visible === 'json' && (
        <ImportAccountsDialog
          open={which === 'json'}
          onOpenChange={handleOpenChange}
          onChanged={bump}
        />
      )}
    </AddAccountContext.Provider>
  );
}
