'use client';

import * as React from 'react';
import {BarChart3, RefreshCw} from 'lucide-react';

import {EmptyState} from '@/components/common/layout/EmptyState';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {AccountUsageTable} from '@/components/common/stats/AccountUsageTable';
import {PerfMatrix} from '@/components/common/stats/PerfMatrix';
import {UsageKpi} from '@/components/common/stats/UsageKpi';
import type {
  AnalyticsData,
  ByAccountRow,
  PerfData,
  Scope,
  UsageSnapshot,
} from '@/components/common/stats/types';
import {Button} from '@/components/ui/button';
import {Skeleton} from '@/components/ui/skeleton';
import {Tabs, TabsList, TabsTrigger} from '@/components/ui/tabs';
import {api} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useRealm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';

/**
 * 数据指标页 —— 旧看板 dashboard.html #pageAnalytics 的新前端版本：
 * 顶部 5 张 KPI（今日 / 累计 Token、平均生成速度、缓存命中率、请求成功率），
 * 下面依次是模型性能矩阵与各账号用量透视。
 */
export default function StatsPage() {
  const {ready: authReady, needsLogin, sessionEpoch} = useAuth();
  const {ready: realmReady, view} = useRealm();

  const [scope, setScope] = React.useState<Scope>('today');
  const [loading, setLoading] = React.useState(true);
  const [analytics, setAnalytics] = React.useState<AnalyticsData | null>(null);
  const [perf, setPerf] = React.useState<PerfData | null>(null);
  const [snapshot, setSnapshot] = React.useState<UsageSnapshot | null>(null);
  const [byAccount, setByAccount] = React.useState<ByAccountRow[]>([]);

  // /usage/analytics 一次就返回「今日 + 全部」两套口径（scope 参数当前由服务端忽略），
  // 所以只在进入页面 / 手动刷新 / 切换出口区域时拉一次，切 Tab 不重扫整份日志。
  const scopeRef = React.useRef<Scope>(scope);
  React.useEffect(() => {
    scopeRef.current = scope;
  }, [scope]);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const [a, p, s, b] = await Promise.all([
        api.usage.analytics(scopeRef.current) as Promise<AnalyticsData>,
        api.usage.perf(view) as Promise<PerfData>,
        api.usage.summary(view) as Promise<UsageSnapshot>,
        api.usage.byAccount() as Promise<{accounts?: ByAccountRow[]}>,
      ]);
      setAnalytics(a);
      setPerf(p);
      setSnapshot(s);
      setByAccount(Array.isArray(b?.accounts) ? b.accounts : []);
    } catch (e) {
      notify.err('数据指标加载失败', e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [view]);

  React.useEffect(() => {
    if (authReady && realmReady && !needsLogin) void load();
  }, [authReady, realmReady, needsLogin, sessionEpoch, load]);

  const hasData = analytics !== null || perf !== null;

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="数据指标"
        description="Token 消耗与推理指标透视：输入 / 输出 / 推理 Token、缓存命中与各账号各模型用量。"
        actions={
          <>
            <Tabs variant="pill" value={scope} onValueChange={(v) => setScope(v as Scope)}>
              <TabsList>
                <TabsTrigger value="today">今日数据</TabsTrigger>
                <TabsTrigger value="all">全部历史</TabsTrigger>
              </TabsList>
            </Tabs>
            <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
              刷新
            </Button>
          </>
        }
      />

      <UsageKpi
        today={analytics?.summary?.today ?? null}
        allTime={analytics?.summary?.all_time ?? null}
        scope={scope}
        loading={loading}
      />

      {loading && !hasData ? (
        <Skeleton className="h-80 w-full rounded-[20px]" />
      ) : !hasData ? (
        <EmptyState
          icon={BarChart3}
          title="暂无用量数据"
          description="还没有读到任何请求记录；产生一次模型调用后再回来看这里。"
        >
          <Button variant="outline" size="sm" onClick={() => void load()}>
            重新加载
          </Button>
        </EmptyState>
      ) : (
        <>
          <PerfMatrix
            analytics={analytics}
            perf={perf}
            snapshot={snapshot}
            scope={scope}
            realm={view}
            loading={loading}
          />
          <AccountUsageTable
            accounts={analytics?.accounts ?? []}
            byAccount={byAccount}
            scope={scope}
            loading={loading}
          />
        </>
      )}
    </div>
  );
}
