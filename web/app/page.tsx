'use client';

import * as React from 'react';
import {Cpu, KeyRound, RefreshCw, ShieldCheck, Users, Zap} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Skeleton} from '@/components/ui/skeleton';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {useAuth} from '@/lib/auth-context';
import {useRealm, type Realm, type RealmMode} from '@/lib/realm-context';
import {api} from '@/lib/api';
import {notify} from '@/lib/toast';
import {AccountTable, type AccountRow} from '@/components/common/gateway/AccountTable';
import {AddAccountBar} from '@/components/common/accounts/AddAccountBar';
import {ModelCatalogTable} from '@/components/common/gateway/ModelCatalogTable';
import {RecentRequestsTable} from '@/components/common/gateway/RecentRequestsTable';
import {SchedulerBar} from '@/components/common/gateway/SchedulerBar';
import {GrowthTasksPanel} from '@/components/common/gateway/GrowthTasksPanel';
import {VmStatusCard} from '@/components/common/gateway/VmStatusCard';

interface UsageSnapshot {
  requests?: number;
  totals?: Record<string, number>;
  [k: string]: unknown;
}

export default function GatewayPage() {
  const {ready: authReady, needsLogin, sessionEpoch} = useAuth();
  const {ready: realmReady, mode, preferred, view, setMode, setView} = useRealm();

  const [accounts, setAccounts] = React.useState<AccountRow[]>([]);
  const [usable, setUsable] = React.useState(0);
  const [usage, setUsage] = React.useState<UsageSnapshot | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [busy, setBusy] = React.useState<string>('');

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const [acc, use] = await Promise.all([
        api.accounts.list('all'),
        api.usage.summary(view),
      ]);
      setAccounts((acc?.accounts as AccountRow[]) || []);
      setUsable(acc?.usable ?? 0);
      setUsage(use as UsageSnapshot);
    } catch (e) {
      notify.err('加载失败', e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [view]);

  React.useEffect(() => {
    // 等 /panel/status 回来、且确实已登录才发请求；登录成功（sessionEpoch
    // 变化）后就地重拉，用户不必手动点刷新。
    if (authReady && realmReady && !needsLogin) void load();
  }, [authReady, realmReady, needsLogin, sessionEpoch, load]);

  const rows = React.useMemo(
    () => accounts.filter((a) => a.realm === view),
    [accounts, view],
  );
  const readyCount = rows.filter((a) => a.enabled && !a.inCooldown).length;
  const checkinPending = rows.filter((a) => a.canCheckin !== false).length;

  const onCheckin = async (uid?: string) => {
    setBusy(uid || 'all');
    try {
      const r = await api.accounts.checkin(uid);
      notify.ok(uid ? '已提交签到' : '已提交全部签到', describeCheckin(r));
      await load();
    } catch (e) {
      notify.err('签到失败', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="网关与运维"
        description="账号池、模型库与调度状态；右上角切换出口区域。"
        actions={
          <>
            <Select value={mode} onValueChange={(v) => void setMode(v as RealmMode)}>
              <SelectTrigger className="h-9 w-[168px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="both">双区（失效自动切换）</SelectItem>
                <SelectItem value="intl">仅国际版</SelectItem>
                <SelectItem value="cn">仅国内版</SelectItem>
              </SelectContent>
            </Select>
            <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
              <RefreshCw className="size-4" />
              刷新
            </Button>
            <Button size="sm" onClick={() => void onCheckin()} disabled={busy === 'all'}>
              <Zap className="size-4" />
              全部签到
            </Button>
          </>
        }
      />

      <SchedulerBar onChanged={load} />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 md:gap-4">
        <StatCard
          label={`${realmLabel(view)}账号`}
          value={loading ? '—' : rows.length}
          hint={`可用 ${readyCount}`}
          icon={Users}
          tone={rows.length ? 'neutral' : 'warning'}
        />
        <StatCard
          label="待签到"
          value={loading ? '—' : checkinPending}
          hint={checkinPending ? '点行内「签到」逐个领取' : '本轮都已领过'}
          icon={ShieldCheck}
          tone={checkinPending ? 'info' : 'success'}
          delay={0.04}
        />
        <StatCard
          label="本区今日请求"
          value={loading ? '—' : (usage?.requests ?? 0)}
          hint={`出口：${realmLabel(view)}`}
          icon={Cpu}
          delay={0.08}
        />
        <StatCard
          label="网关出口模式"
          value={modeLabel(mode)}
          hint={`优先 ${realmLabel(preferred)}`}
          icon={KeyRound}
          tone={mode === 'both' ? 'accent' : 'neutral'}
          delay={0.12}
        />
      </div>

      <AddAccountBar onChanged={load} />

      {loading && accounts.length === 0 ? (
        <Skeleton className="h-64 w-full rounded-[20px]" />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={Users}
          title={`暂无${realmLabel(view)}账号`}
          description="用「扫描本机凭证」或 OAuth / PAT 导入账号后即可开始转发。"
        />
      ) : (
        <AccountTable rows={rows} busy={busy} onCheckin={onCheckin} onChanged={load} />
      )}

      <RecentRequestsTable realm={view} />

      <ModelCatalogTable realm={view} />

      <GrowthTasksPanel realm={view} onChanged={load} />
      <VmStatusCard realm={view} />

      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="secondary">{realmLabel(view)}</Badge>
        <button
          type="button"
          className="underline underline-offset-2"
          onClick={() => setView((view === 'cn' ? 'intl' : 'cn') as Realm)}
        >
          切换到{realmLabel(view === 'cn' ? 'intl' : 'cn')}
        </button>
      </div>
    </div>
  );
}

function realmLabel(realm: string) {
  return realm === 'intl' ? '国际版' : '国内版';
}

function modeLabel(mode: string) {
  return mode === 'both' ? '双区' : mode === 'intl' ? '仅国际' : '仅国内';
}

function describeCheckin(r: unknown): string | undefined {
  const data = r as {results?: Array<{uid?: string; ok?: boolean; reward_text?: string}>};
  if (!data?.results?.length) return undefined;
  const okCount = data.results.filter((x) => x.ok).length;
  return `${okCount}/${data.results.length} 个账号成功`;
}
