'use client';

import * as React from 'react';
import {ChevronDown, ChevronUp, Clock, Loader2, Pause, Play} from 'lucide-react';

import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Skeleton} from '@/components/ui/skeleton';
import {api} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useAuthedLoad} from '@/lib/use-authed-load';
import {cn} from '@/lib/utils';

/**
 * 调度器状态条（「网关与运维」页）。
 *
 * 数据来自 GET /scheduler（qoder2api/scheduler.py 的 status()），
 * 行为对齐旧看板 dashboard.html 的调度器状态栏：
 *   · 显示运行中 / 已暂停、排程说明、上次 / 下次执行时间与最近调度日志；
 *   · 「立即触发」= POST /scheduler/trigger（后台异步执行，日志稍后刷新）；
 *   · 「暂停 / 启用」= POST /scheduler/toggle（后端按当前状态翻转，
 *     这里以响应里的 enabled 为准，再回读一次状态）。
 */

interface SchedulerStatus {
  enabled?: boolean;
  mode?: string;
  mode_cn?: string;
  mode_intl?: string;
  last_run_time?: string;
  next_run_time?: string;
  logs?: string[];
  ok?: boolean;
  msg?: string;
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function SchedulerBar({onChanged}: {onChanged: () => Promise<void> | void}) {
  const [status, setStatus] = React.useState<SchedulerStatus | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState<'' | 'trigger' | 'toggle'>('');
  const [logsOpen, setLogsOpen] = React.useState(false);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = React.useCallback(async (quiet = false) => {
    try {
      const s = (await api.scheduler.get()) as SchedulerStatus;
      setStatus(s || null);
    } catch (e) {
      if (!quiet) notify.err('调度器状态加载失败', errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useAuthedLoad(load, [load]);
  React.useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const trigger = async () => {
    if (busy) return;
    setBusy('trigger');
    try {
      const r = (await api.scheduler.trigger()) as {ok?: boolean; msg?: string};
      if (r?.ok) {
        notify.ok('已触发调度巡检', r.msg || '后台正在执行签到 / 保活');
      } else {
        notify.warn('未能触发', r?.msg || '已有巡检正在执行，请稍候再试');
      }
      await load(true);
      // 后端在后台线程里跑，稍后再抓一次日志，让「最近调度日志」跟上
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => void load(true), 3000);
      await onChanged();
    } catch (e) {
      notify.err('触发失败', errText(e));
    } finally {
      setBusy('');
    }
  };

  const toggle = async () => {
    if (busy) return;
    const current = status?.enabled === true;
    setBusy('toggle');
    try {
      const r = (await api.scheduler.toggle(!current)) as SchedulerStatus;
      if (r?.ok === false) {
        // 调度器未初始化时后端返回 {ok:false, msg}，别把它当成功
        notify.warn('未能切换调度器', r.msg || '调度器未初始化');
        await load(true);
        return;
      }
      // 后端 toggle 是「翻转」语义，可能忽略请求体里的 enabled，以响应为准
      const nowEnabled = typeof r?.enabled === 'boolean' ? r.enabled : !current;
      notify.ok(
        nowEnabled ? '调度器已启用' : '调度器已暂停',
        nowEnabled ? '将按整点排程自动巡检' : '自动签到与保活已停止，可随时手动触发',
      );
      await load(true);
      await onChanged();
    } catch (e) {
      notify.err('切换失败', errText(e));
    } finally {
      setBusy('');
    }
  };

  if (loading && !status) {
    return <Skeleton className="h-[86px] w-full rounded-[20px]" />;
  }

  const enabled = status?.enabled === true;
  const logs = status?.logs || [];
  const shown = (logsOpen ? logs : logs.slice(-3)).slice().reverse(); // 最新在上

  return (
    <section className="rounded-[20px] bg-muted px-3.5 py-3 sm:px-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
          <span className="flex items-center gap-1.5 text-sm font-medium">
            <span
              className={cn(
                'size-2 shrink-0 rounded-full',
                enabled ? 'bg-emerald-500' : 'bg-gray-400 dark:bg-gray-500',
              )}
            />
            后台定时调度器
          </span>
          <Badge
            variant="outline"
            className={cn(
              'rounded-full text-[11px]',
              enabled
                ? 'border-emerald-600/30 bg-emerald-600/10 text-emerald-700 dark:text-emerald-400'
                : 'border-border/60 text-muted-foreground',
            )}
          >
            {enabled ? '运行中' : '已暂停'}
          </Badge>
          <span className="text-[11px] text-muted-foreground">
            {status?.mode_cn || status?.mode || status?.msg || '等待调度器状态'}
          </span>
          <span className="flex flex-wrap items-center gap-1 text-[11px] text-muted-foreground">
            <Clock className="size-3.5" />
            上次 {status?.last_run_time || '—'} · 下次 {status?.next_run_time || '—'}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="outline"
            size="sm"
            className="h-7 rounded-full text-xs"
            disabled={busy !== ''}
            title="立刻在后台执行一轮签到 / 保活巡检"
            onClick={() => void trigger()}
          >
            {busy === 'trigger' ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Play className="size-3.5" />
            )}
            立即触发
          </Button>
          <Button
            variant={enabled ? 'outline' : 'default'}
            size="sm"
            className="h-7 rounded-full text-xs"
            disabled={busy !== ''}
            title={enabled ? '暂停整点排程（手动触发仍可用）' : '恢复整点排程'}
            onClick={() => void toggle()}
          >
            {busy === 'toggle' ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Pause className="size-3.5" />
            )}
            {enabled ? '暂停调度' : '启用调度'}
          </Button>
        </div>
      </div>

      {logs.length > 0 && (
        <div className="mt-2 border-t border-border/40 pt-1.5">
          <button
            type="button"
            onClick={() => setLogsOpen((v) => !v)}
            className="flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
          >
            {logsOpen ? (
              <ChevronUp className="size-3.5" />
            ) : (
              <ChevronDown className="size-3.5" />
            )}
            最近调度日志（{logs.length} 条{logsOpen ? '' : '，当前显示最新 3 条'}）
          </button>
          <div
            className={cn(
              'mt-1 space-y-0.5',
              logsOpen && 'scroll-slim max-h-40 overflow-y-auto',
            )}
          >
            {shown.map((line, i) => (
              <div
                key={`${i}-${line.slice(0, 24)}`}
                className="truncate font-mono text-[10.5px] leading-4 text-muted-foreground"
                title={line}
              >
                {line}
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
