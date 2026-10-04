'use client';

import * as React from 'react';
import {Activity, Cpu, Database, RefreshCw, Users, Zap} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import {Button} from '@/components/ui/button';
import {Skeleton} from '@/components/ui/skeleton';
import {GatewayExitCard} from '@/components/common/dashboard/GatewayExitCard';
import {UsageTrendChart, type DailyPoint} from '@/components/common/dashboard/UsageTrendChart';
import {RecentRequestsTable} from '@/components/common/gateway/RecentRequestsTable';
import {SchedulerBar} from '@/components/common/gateway/SchedulerBar';
import type {AccountRow} from '@/components/common/gateway/AccountTable';
import {api} from '@/lib/api';
import {useAddAccount} from '@/lib/add-account-context';
import {notify} from '@/lib/toast';
import {useAuthedLoad} from '@/lib/use-authed-load';
import {useRealm} from '@/lib/realm-context';

/**
 * `/` —— 仪表盘（总览）。
 *
 * 结构对齐 workbuddy-manager 的 dashboard：统计卡 + 趋势图 + 状态栏。
 * 「国际版 / 国内版」由右上角 RealmToggle 切换，本页数据（账号数 / 14 天
 * 趋势 / 最近请求）跟随该区域；网关出口模式是全局设置，单独放在顶部出口卡
 * （四选一，与旧看板 realmModeSelect 一致）。
 */

export default function DashboardPage() {
  const {view} = useRealm();
  const {refreshKey} = useAddAccount();
  const [accounts, setAccounts] = React.useState<AccountRow[]>([]);
  const [daily, setDaily] = React.useState<DailyPoint[]>([]);
  const [loading, setLoading] = React.useState(true);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const [acc, d] = await Promise.all([
        api.accounts.list('all'),
        api.usage.daily(14, view),
      ]);
      setAccounts((acc?.accounts as AccountRow[]) || []);
      setDaily((d?.days as DailyPoint[]) || []);
    } catch (e) {
      notify.err('仪表盘加载失败', e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [view]);

  useAuthedLoad(load, [load, refreshKey]);

  const rows = React.useMemo(() => accounts.filter((a) => a.realm === view), [accounts, view]);
  const readyCount = rows.filter((a) => a.enabled && !a.inCooldown).length;
  const today = daily.length ? daily[daily.length - 1] : null;
  const cachePct =
    today && today.prompt_tokens > 0
      ? Math.round((today.cached_tokens * 100) / today.prompt_tokens)
      : null;
  const avgSpeed =
    today && today.speed_n > 0 ? (today.speed_sum / today.speed_n).toFixed(1) : null;
  const avgTtft = today && today.ttft_n > 0 ? Math.round(today.ttft_sum / today.ttft_n) : null;
  const realmLabel = view === 'intl' ? '国际版' : '国内版';

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="仪表盘"
        description={`网关总览 · 当前查看 ${realmLabel}（右上角切换区域）`}
        actions={
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
            刷新
          </Button>
        }
      />

      <GatewayExitCard />

      <SchedulerBar onChanged={load} />

      <div className="grid grid-cols-2 gap-3 md:gap-4 lg:grid-cols-5">
        <StatCard
          label={`${realmLabel}账号`}
          value={loading && !accounts.length ? '—' : rows.length}
          hint={`可用 ${readyCount}`}
          icon={Users}
          tone={rows.length ? 'neutral' : 'warning'}
        />
        <StatCard
          label="今日请求"
          value={loading && !today ? '—' : (today?.requests ?? 0)}
          hint={today?.failed ? `失败 ${today.failed}` : '无失败'}
          hintTone={today?.failed ? 'danger' : undefined}
          icon={Activity}
          delay={0.05}
        />
        <StatCard
          label="今日 Token"
          value={fmt(today?.tokens ?? 0)}
          hint={`输入 ${fmt(today?.prompt_tokens ?? 0)}`}
          icon={Cpu}
          tone="info"
          delay={0.1}
        />
        <StatCard
          label="缓存命中率"
          value={cachePct == null ? '—' : `${cachePct}%`}
          hint={`缓存 ${fmt(today?.cached_tokens ?? 0)} tokens`}
          icon={Database}
          tone={cachePct != null && cachePct >= 50 ? 'success' : 'neutral'}
          delay={0.15}
        />
        <StatCard
          label="平均生成速度"
          value={avgSpeed ? `${avgSpeed} tok/s` : '—'}
          hint={avgTtft != null ? `平均首字 ${avgTtft} ms` : '今日暂无样本'}
          icon={Zap}
          tone="accent"
          delay={0.2}
        />
      </div>

      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="text-[11px] font-medium text-muted-foreground">
            近 14 天请求趋势（{realmLabel}）
          </div>
          <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <span className="h-0.5 w-4 rounded bg-[var(--chart-1)]" /> 请求
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-0.5 w-4 rounded bg-[var(--destructive)]" /> 失败
            </span>
          </div>
        </div>
        {loading && daily.length === 0 ? (
          <Skeleton className="h-[220px] w-full rounded-xl" />
        ) : (
          <UsageTrendChart data={daily} />
        )}
      </section>

      <RecentRequestsTable realm={view} />
    </div>
  );
}

function fmt(n: number): string {
  return (n ?? 0).toLocaleString('en-US');
}
