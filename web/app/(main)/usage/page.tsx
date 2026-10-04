'use client';

import * as React from 'react';
import {Activity, BarChart3, Boxes, Coins, KeyRound, Percent, RefreshCw} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import {fmt} from '@/components/common/stats/format';
import {BreakdownPanel, type BreakdownRow} from '@/components/common/usage/BreakdownPanel';
import {UsageTrendCard} from '@/components/common/usage/UsageTrendCard';
import {RecentRequestsTable} from '@/components/common/gateway/RecentRequestsTable';
import {
  fmtCompact,
  keyTitle,
  type UsageGroup,
  type UsageMetric,
  type UsageRangeKey,
  type UsageStats,
} from '@/components/common/usage/types';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Skeleton} from '@/components/ui/skeleton';
import {api} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useRealm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';

/**
 * `/usage` —— 用量统计（底部栏「治理」组，位于「统计」左侧）。
 *
 * 数据来自 SQLite 聚合库（qoder2api/usagedb.py，`/usage/stats`），支持：
 *   · 时间范围：今天（按小时）/ 近 7 天 / 近 30 天 / 自定义起止日期；
 *   · 维度：按模型、按密钥（请求数 / Token / 积分），趋势可按总量/模型/密钥分组；
 *   · 每 60 秒自动刷新（标签页隐藏时跳过，切回立刻刷新一次）。
 * 区域跟随右上角的国际版/国内版切换（与其它页面一致）。
 *
 * 「统计」页看的是延迟/性能（首字、速度、缓存命中），本页看的是用量与账单口径；
 * 两者共用同一份 usage.jsonl，本页的聚合结果持久化在 usage/usage.db（无限期保留）。
 */

const RANGE_OPTIONS: {value: UsageRangeKey; label: string}[] = [
  {value: 'today', label: '今天'},
  {value: '7d', label: '近 7 天'},
  {value: '30d', label: '近 30 天'},
  {value: 'custom', label: '自定义'},
];
const AUTO_REFRESH_MS = 60_000;

const isoDay = (offsetDays = 0) =>
  new Date(Date.now() + offsetDays * 86_400_000).toISOString().slice(0, 10);
const clockNow = () => new Date().toLocaleTimeString('zh-CN', {hour12: false});

export default function UsagePage() {
  const {ready: authReady, needsLogin, sessionEpoch} = useAuth();
  const {ready: realmReady, view} = useRealm();

  const [rangeKey, setRangeKey] = React.useState<UsageRangeKey>('today');
  const [from, setFrom] = React.useState(() => isoDay(-6));
  const [to, setTo] = React.useState(() => isoDay(0));
  const [group, setGroup] = React.useState<UsageGroup>('total');
  const [metric, setMetric] = React.useState<UsageMetric>('tokens');

  const [data, setData] = React.useState<UsageStats | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [updatedAt, setUpdatedAt] = React.useState('');

  // 请求参数用 ref 转发给定时器：切范围/切区域后 60 秒轮询必须跟着变，
  // 但定时器本身不该因每次参数变化重建（否则连续操作时永远等不到那一分钟）。
  const paramsRef = React.useRef({rangeKey, from, to, group, view});

  const load = React.useCallback(async (silent = false) => {
    const p = paramsRef.current;
    if (!silent) setLoading(true);
    try {
      const r = (await api.usage.stats({
        range: p.rangeKey,
        from: p.rangeKey === 'custom' ? p.from : undefined,
        to: p.rangeKey === 'custom' ? p.to : undefined,
        realm: p.view,
        group: p.group,
      })) as UsageStats;
      setData(r);
      setUpdatedAt(clockNow());
    } catch (e) {
      // 静默轮询失败只保留旧数据（不打扰用户），手动/首屏加载失败才提示
      if (!silent) {
        notify.err('用量统计加载失败', e instanceof Error ? e.message : String(e));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    paramsRef.current = {rangeKey, from, to, group, view};
  }, [rangeKey, from, to, group, view]);

  React.useEffect(() => {
    if (authReady && realmReady && !needsLogin) void load();
  }, [authReady, realmReady, needsLogin, sessionEpoch, rangeKey, from, to, group, view, load]);

  // 每 60 秒自动刷新：页面隐藏时跳过（省电、避免无意义上游查询），
  // 切回可见时立刻补一次，不至于盯着一小时前的数字。
  React.useEffect(() => {
    const tick = () => {
      if (document.visibilityState === 'visible') void load(true);
    };
    const timer = setInterval(tick, AUTO_REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void load(true);
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [load]);

  const s = data?.summary;
  const realmLabel = view === 'intl' ? '国际版' : '国内版';
  const modelRows: BreakdownRow[] = React.useMemo(
    () =>
      (data?.by_model ?? []).map((r) => ({
        name: r.model || '(未知模型)',
        requests: r.requests,
        total_tokens: r.total_tokens,
        prompt_tokens: r.prompt_tokens,
        completion_tokens: r.completion_tokens,
        credit: r.credit,
      })),
    [data],
  );
  const keyRows: BreakdownRow[] = React.useMemo(
    () =>
      (data?.by_key ?? []).map((r) => ({
        name: keyTitle(r),
        requests: r.requests,
        total_tokens: r.total_tokens,
        prompt_tokens: r.prompt_tokens,
        completion_tokens: r.completion_tokens,
        credit: r.credit,
      })),
    [data],
  );

  const avgPerRequest = s && s.requests > 0 ? Math.round(s.total_tokens / s.requests) : 0;
  const customInvalid = rangeKey === 'custom' && from > to;

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="用量统计"
        description={`${realmLabel}的 Token 消耗与请求量（SQLite 持久化，无限期保留）· 每 60 秒自动刷新${
          updatedAt ? ` · 更新于 ${updatedAt}` : ''
        }`}
        actions={
          <>
            <Select
              value={rangeKey}
              onValueChange={(v) => setRangeKey(v as UsageRangeKey)}
            >
              <SelectTrigger size="sm" className="w-[120px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RANGE_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {rangeKey === 'custom' && (
              <div className="flex items-center gap-1.5">
                <Input
                  type="date"
                  value={from}
                  max={to || isoDay(0)}
                  onChange={(e) => setFrom(e.target.value)}
                  className="h-8 w-[140px] text-xs"
                  aria-label="开始日期"
                />
                <span className="text-xs text-muted-foreground">至</span>
                <Input
                  type="date"
                  value={to}
                  min={from}
                  max={isoDay(0)}
                  onChange={(e) => setTo(e.target.value)}
                  className="h-8 w-[140px] text-xs"
                  aria-label="结束日期"
                />
              </div>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => void load()}
              disabled={loading || customInvalid}
            >
              <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
              刷新
            </Button>
          </>
        }
      />

      {customInvalid && (
        <p className="text-xs text-amber-600 dark:text-amber-400">
          自定义范围不合法：开始日期不能晚于结束日期。
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 md:gap-4 lg:grid-cols-4">
        {loading && !data ? (
          Array.from({length: 4}).map((_, i) => (
            <Skeleton key={i} className="h-[96px] rounded-[20px]" />
          ))
        ) : (
          <>
            <StatCard
              label="请求数"
              value={fmt(s?.requests)}
              hint={
                s?.failed
                  ? `失败 ${fmt(s.failed)} · 成功 ${fmt(Math.max(0, (s?.requests ?? 0) - s.failed))}`
                  : '失败 0'
              }
              hintTone={s?.failed ? 'danger' : undefined}
              icon={Activity}
              tone="info"
            />
            <StatCard
              label="Token 消耗"
              value={fmtCompact(s?.total_tokens)}
              hint={`输入 ${fmt(s?.prompt_tokens)} · 输出 ${fmt(s?.completion_tokens)}`}
              icon={Coins}
              tone="accent"
              delay={0.04}
            />
            <StatCard
              label="缓存命中率"
              value={`${(s?.cache_hit_pct ?? 0).toFixed(1)}%`}
              hint={`缓存 ${fmt(s?.cached_tokens)} · 思考 ${fmt(s?.reasoning_tokens)}`}
              icon={Percent}
              tone="success"
              delay={0.08}
            />
            <StatCard
              label="积分消耗"
              value={(s?.credit ?? 0).toFixed(2)}
              hint={avgPerRequest ? `平均 ${fmt(avgPerRequest)} tokens / 请求` : '尚无请求'}
              icon={BarChart3}
              tone="warning"
              delay={0.12}
            />
          </>
        )}
      </div>

      <UsageTrendCard
        data={data}
        loading={loading}
        group={group}
        metric={metric}
        onGroupChange={setGroup}
        onMetricChange={setMetric}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <BreakdownPanel
          title="按模型"
          icon={Boxes}
          rows={modelRows}
          loading={loading}
          emptyText="该时间范围内没有模型用量"
        />
        <BreakdownPanel
          title="按密钥"
          icon={KeyRound}
          rows={keyRows}
          loading={loading}
          emptyText="该时间范围内没有密钥用量"
        />
      </div>

      {/* 全量请求记录：仪表盘的「最近请求」只是最近 100 条的预览。 */}
      <RecentRequestsTable realm={view} title="全部请求记录" hint="全部历史" />
    </div>
  );
}
