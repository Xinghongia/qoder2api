'use client';

import * as React from 'react';
import type {ReactNode} from 'react';
import {ExternalLink, LogOut, RefreshCw} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {api, http} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';
import {MonoValue, SectionCard, errText, type SettingsSnapshot, type UpdateInfo} from './shared';

/**
 * 运行信息：版本 / 新版本检测 / 目录 / 退出面板登录。
 *
 * 新版本检测走后端 GET /update/check（缓存 6h）：首屏自动查一次（失败静默），
 * 「立即检查」带 force=1 绕过缓存（与旧看板 loadUpdateState(true) 一致）。
 */
export function RuntimeSection({data}: {data: SettingsSnapshot}) {
  const {logout} = useAuth();
  const [update, setUpdate] = React.useState<UpdateInfo | null>(null);
  const [checking, setChecking] = React.useState(false);
  const [checkError, setCheckError] = React.useState('');

  const check = React.useCallback(async (force: boolean) => {
    setChecking(true);
    setCheckError('');
    try {
      // api.updateCheck() 不带 force；手动「立即检查」需要绕过 6 小时缓存，
      // 因此走同一客户端的 http 直发（api.ts 不在本次改动范围内）。
      const r = (force
        ? await http.get('/update/check?force=1')
        : await api.updateCheck()) as UpdateInfo;
      setUpdate(r);
    } catch (e) {
      setUpdate(null);
      setCheckError(errText(e));
    } finally {
      setChecking(false);
    }
  }, []);

  React.useEffect(() => {
    void check(false);
  }, [check]);

  let updateNode: ReactNode = '尚未检查';
  let updateTone = 'text-muted-foreground';
  let updateLink: ReactNode = null;
  if (checking) {
    updateNode = '正在检查…';
  } else if (checkError) {
    updateNode = `检查失败：${checkError}`;
    updateTone = 'text-amber-600 dark:text-amber-400';
  } else if (update) {
    if (!update.ok) {
      updateNode = '检查失败（网络不可达或 GitHub 受限）';
      updateTone = 'text-amber-600 dark:text-amber-400';
    } else if (update.no_releases) {
      updateNode = `仓库暂无发布记录（当前 ${update.current || data.version}）`;
    } else if (update.has_update) {
      updateNode = (
        <>
          发现新版本 <span className="font-semibold text-foreground">{update.latest}</span>
          （当前 {update.current}）
        </>
      );
      updateTone = 'text-amber-600 dark:text-amber-400';
      if (update.url) {
        updateLink = (
          <a
            href={update.url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-primary underline underline-offset-2"
          >
            前往 Releases 下载升级
            <ExternalLink className="size-3" />
          </a>
        );
      }
    } else {
      updateNode = `已是最新版本（${update.current}${update.latest ? ` · 最新 ${update.latest}` : ''}）`;
      updateTone = 'text-emerald-600 dark:text-emerald-400';
    }
  }

  return (
    <SectionCard title="运行信息" delay={0.16}>
      <div className="space-y-2.5">
        <InfoRow label="当前版本">
          <MonoValue>{data.version || '-'}</MonoValue>
        </InfoRow>
        <InfoRow label="新版本检测">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <span className={cn('text-xs', updateTone)}>{updateNode}</span>
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-2 text-[11px]"
              onClick={() => void check(true)}
              disabled={checking}
            >
              <RefreshCw className={cn('size-3.5', checking && 'animate-spin')} />
              立即检查
            </Button>
            {updateLink}
          </div>
        </InfoRow>
        <InfoRow label="账号存储目录">
          <MonoValue className="text-muted-foreground">{data.accounts_dir || '-'}</MonoValue>
        </InfoRow>
        <InfoRow label="用量日志目录">
          <MonoValue className="text-muted-foreground">{data.usage_dir || '-'}</MonoValue>
        </InfoRow>
        <InfoRow label="设置文件">
          <MonoValue className="text-muted-foreground">{data.settings_file || '-'}</MonoValue>
        </InfoRow>
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={() => void logout()}>
          <LogOut className="size-4" />
          退出面板登录
        </Button>
      </div>
    </SectionCard>
  );
}

function InfoRow({label, children}: {label: string; children: ReactNode}) {
  return (
    <div className="grid gap-1 rounded-xl bg-background/70 px-3 py-2 sm:grid-cols-[140px_1fr] sm:items-center dark:bg-white/[0.04]">
      <div className="text-[11px] font-medium text-muted-foreground">{label}</div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}
