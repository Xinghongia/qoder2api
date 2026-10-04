'use client';

import * as React from 'react';
import {useRouter} from 'next/navigation';
import {Loader2} from 'lucide-react';

import {AppDock} from '@/components/common/layout/AppDock';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {AddAccountProvider} from '@/lib/add-account-context';
import {useAuth} from '@/lib/auth-context';

/**
 * 主布局：登录守卫 + 内容容器 + 底部管理栏。
 *
 * 静态导出下没有 middleware，守卫只能在客户端做：`/panel/status` 回来之前
 * 不渲染业务内容（避免一片 401 空态），确认未登录就 replace 到 /login。
 */
export default function MainLayout({children}: {children: React.ReactNode}) {
  const {ready, needsLogin} = useAuth();
  const router = useRouter();

  React.useEffect(() => {
    if (ready && needsLogin) router.replace('/login');
  }, [ready, needsLogin, router]);

  if (!ready || needsLogin) {
    return (
      <div className="grid min-h-svh place-items-center">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          {ready ? '正在跳转登录…' : '正在连接网关…'}
        </div>
      </div>
    );
  }

  return (
    <AddAccountProvider>
      <div className="@container/main flex min-h-screen flex-col">
        {/* 右上角：查看区域切换（国际版 / 国内版），作用于仪表盘/账号/模型/统计 */}
        <div className="pointer-events-none fixed right-4 top-4 z-30 sm:right-6 md:right-8 lg:right-12">
          <div className="pointer-events-auto">
            <RealmToggle />
          </div>
        </div>
        <div className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-4 px-4 pb-28 pt-8 sm:px-6 md:gap-6 md:px-8 lg:px-12">
          {children}
        </div>
      </div>
      <AppDock />
    </AddAccountProvider>
  );
}
