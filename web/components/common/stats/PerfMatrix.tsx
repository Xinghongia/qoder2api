'use client';

import * as React from 'react';
import {ChartColumn} from 'lucide-react';

import {EmptyState} from '@/components/common/layout/EmptyState';
import {Badge} from '@/components/ui/badge';
import {Skeleton} from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {cn} from '@/lib/utils';
import {fmt, fmtMs, fmtPct, NUM, realmLabel, scopeLabel} from './format';
import type {
  AnalyticsData,
  PerfData,
  PerfModelStat,
  Scope,
  UsageSnapshot,
  UsageStat,
} from './types';

interface MatrixAccount {
  uid: string;
  name: string;
  realm: string;
  count: number;
}

interface MatrixRow {
  id: string;
  /** 当前口径（今日 / 全部）下该模型的 Token 统计，来自 /usage/analytics。 */
  stat: UsageStat | null;
  /** 延迟 / 速度采样，来自 /usage/perf（按当前出口区域过滤，最近 5000 条）。 */
  perf: PerfModelStat | null;
  accounts: MatrixAccount[];
  realms: string[];
  /** 相对用量最大模型的占比（进度条）。 */
  share: number;
}

const MAX_ACCOUNT_CHIPS = 5;

/**
 * 模型性能指标与用量一览（对齐旧看板 #perfMatrix）。
 *
 * 列：模型名称 / 区域 / 账号 / 请求数 / 总 Token / 输入-输出(思考) /
 * 首字延迟 (TTFT) / 生成速度 / 端到端耗时 / 缓存命中率 / 用量占比。
 * Token 口径跟随「今日 / 全部」切换；延迟与速度是采样数据（每次请求实时记录）。
 */
export function PerfMatrix({
  analytics,
  perf,
  snapshot,
  scope,
  realm,
  loading,
}: {
  analytics: AnalyticsData | null;
  perf: PerfData | null;
  snapshot: UsageSnapshot | null;
  scope: Scope;
  realm: string;
  loading: boolean;
}) {
  const rows = React.useMemo<MatrixRow[]>(() => {
    const map = new Map<string, MatrixRow>();

    const pick = (m: {today: UsageStat; all_time: UsageStat}) =>
      scope === 'today' ? m.today : m.all_time;

    for (const m of analytics?.models ?? []) {
      if (!m?.model || m.model.endsWith('-model')) continue;
      map.set(m.model, {
        id: m.model,
        stat: pick(m),
        perf: perf?.by_model?.[m.model] ?? null,
        accounts: [],
        realms: [],
        share: 0,
      });
    }

    // perf 采样里有、analytics 里没有的模型也补一行，别让延迟数据凭空消失。
    for (const [id, p] of Object.entries(perf?.by_model ?? {})) {
      if (!id || id.endsWith('-model') || map.has(id) || !p) continue;
      if ((p.requests ?? 0) <= 0) continue;
      map.set(id, {id, stat: null, perf: p, accounts: [], realms: [], share: 0});
    }

    const list = Array.from(map.values()).filter((r) => {
      const touched = (r.stat?.requests ?? 0) + (r.stat?.errors ?? 0);
      return touched > 0 || (r.perf?.requests ?? 0) > 0;
    });

    const maxTokens = Math.max(1, ...list.map((r) => r.stat?.total_tokens ?? 0));
    for (const row of list) {
      row.share = Math.round(((row.stat?.total_tokens ?? 0) / maxTokens) * 100);
      const used = snapshot?.by_model?.[row.id]?.accounts;
      if (!used) continue;
      const realms = new Set<string>();
      for (const [uid, count] of Object.entries(used)) {
        const info = snapshot?.accounts_map?.[uid];
        if (info?.realm) realms.add(info.realm);
        row.accounts.push({
          uid,
          name: info?.nickname || uid.slice(0, 8),
          realm: info?.realm || '',
          count,
        });
      }
      row.accounts.sort((x, y) => y.count - x.count);
      row.realms = Array.from(realms);
    }

    return list.sort(
      (x, y) =>
        (y.stat?.total_tokens ?? 0) - (x.stat?.total_tokens ?? 0) ||
        (y.perf?.requests ?? 0) - (x.perf?.requests ?? 0),
    );
  }, [analytics, perf, snapshot, scope]);

  const total = scope === 'today' ? analytics?.summary?.today : analytics?.summary?.all_time;

  if (loading && !analytics && !perf) {
    return <Skeleton className="h-80 w-full rounded-[20px]" />;
  }

  return (
    <section className="rounded-[20px] bg-muted p-3 md:p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2 px-1">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <ChartColumn className="size-4 text-muted-foreground" />
            模型性能指标与用量一览
            {rows.length > 0 && (
              <span className="text-xs font-normal text-muted-foreground">
                （有调用的模型 {rows.length} 个）
              </span>
            )}
          </h2>
          <p className="mt-1 text-[11px] text-muted-foreground">
            融合首字延迟 (TTFT)、生成速度、耗时与 Token 统计；Token 口径：
            {scopeLabel(scope)}（全部区域），延迟/速度采样：{realmLabel(realm)} · 最近 5000 条。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
          <LegendDot className="bg-emerald-500" label="生成速度 tok/s" />
          <LegendDot className="bg-amber-500" label="思考 Token" />
          <LegendDot className="bg-violet-500" label="总 Token 占比" />
        </div>
      </div>

      {rows.length === 0 ? (
        <EmptyState
          icon={ChartColumn}
          title="暂无模型用量"
          description="还没有记录到任何模型请求；产生一次调用后这里会显示各模型指标。"
          className="flex flex-col items-center justify-center p-8 text-center"
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow className="border-border/40 hover:bg-transparent">
              <TableHead className="text-[11px] text-muted-foreground">模型名称</TableHead>
              <TableHead className="w-[76px] text-center text-[11px] text-muted-foreground">区域</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">账号</TableHead>
              <TableHead className="text-right text-[11px] text-muted-foreground">请求数</TableHead>
              <TableHead className="text-right text-[11px] text-muted-foreground">总 Token</TableHead>
              <TableHead className="text-right text-[11px] text-muted-foreground">输入 / 输出 (思考)</TableHead>
              <TableHead className="text-right text-[11px] text-muted-foreground">首字延迟 (TTFT)</TableHead>
              <TableHead className="text-right text-[11px] text-muted-foreground">生成速度</TableHead>
              <TableHead className="text-right text-[11px] text-muted-foreground">端到端耗时</TableHead>
              <TableHead className="text-right text-[11px] text-muted-foreground">缓存命中率</TableHead>
              <TableHead className="min-w-[110px] text-[11px] text-muted-foreground">用量占比</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow className="border-border/40 bg-black/[0.03] font-medium hover:bg-black/[0.03] dark:bg-white/[0.04] dark:hover:bg-white/[0.04]">
              <TableCell className="font-semibold">全部模型合计 (Total)</TableCell>
              <TableCell className="text-center text-muted-foreground">—</TableCell>
              <TableCell className="text-muted-foreground">全部账号</TableCell>
              <TableCell className={cn('text-right', NUM)}>{fmt(total?.requests)}</TableCell>
              <TableCell className={cn('text-right font-semibold text-violet-600 dark:text-violet-400', NUM)}>
                {fmt(total?.total_tokens)}
              </TableCell>
              <TableCell className={cn('text-right', NUM)}>
                {fmt(total?.prompt_tokens)} / {fmt(total?.completion_tokens)}
                {!!total?.reasoning_tokens && (
                  <span className="ml-1 text-amber-600 dark:text-amber-400">
                    ({fmt(total.reasoning_tokens)})
                  </span>
                )}
              </TableCell>
              <TableCell className={cn('text-right', NUM)}>{perfTtft(perf?.ttft_ms ?? null)}</TableCell>
              <TableCell className={cn('text-right', NUM)}>{perfSpeed(perf?.tokens_per_sec ?? null)}</TableCell>
              <TableCell className={cn('text-right', NUM)}>
                {perf?.wall_ms ? fmtMs(perf.wall_ms.avg) : '—'}
              </TableCell>
              <TableCell className={cn('text-right', NUM)}>
                {perf?.cache_hit_pct ? fmtPct(perf.cache_hit_pct.avg) : '—'}
              </TableCell>
              <TableCell className={NUM}>
                <ShareBar pct={100} />
              </TableCell>
            </TableRow>

            {rows.map((row) => (
              <TableRow key={row.id} className="border-border/40">
                <TableCell className="font-medium">
                  <span className="font-mono">{row.id}</span>
                  {!!row.perf?.errors && (
                    <span className="ml-1.5 rounded bg-red-500/10 px-1.5 py-0.5 text-[10px] font-normal text-red-600 dark:text-red-400">
                      {row.perf.errors} 失败
                    </span>
                  )}
                </TableCell>
                <TableCell className="text-center">
                  <RealmCell realms={row.realms} fallback={realm} />
                </TableCell>
                <TableCell>{accountsCell(row.accounts)}</TableCell>
                <TableCell className={cn('text-right', NUM)}>{fmt(row.stat?.requests)}</TableCell>
                <TableCell className={cn('text-right font-semibold', NUM)}>{fmt(row.stat?.total_tokens)}</TableCell>
                <TableCell className={cn('text-right', NUM)}>
                  {fmt(row.stat?.prompt_tokens)} / {fmt(row.stat?.completion_tokens)}
                  {!!row.stat?.reasoning_tokens && (
                    <span className="ml-1 text-amber-600 dark:text-amber-400">
                      ({fmt(row.stat.reasoning_tokens)})
                    </span>
                  )}
                </TableCell>
                <TableCell className={cn('text-right', NUM)}>{perfTtft(row.perf?.ttft_ms ?? null)}</TableCell>
                <TableCell className={cn('text-right', NUM)}>{perfSpeed(row.perf?.tokens_per_sec ?? null)}</TableCell>
                <TableCell className={cn('text-right', NUM)}>
                  {row.perf?.wall_ms ? fmtMs(row.perf.wall_ms.avg) : '—'}
                </TableCell>
                <TableCell className={cn('text-right', NUM)}>{cachePct(row.stat, row.perf)}</TableCell>
                <TableCell className={NUM}>
                  <ShareBar pct={row.share} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

function LegendDot({className, label}: {className: string; label: string}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <i className={cn('size-2 rounded-full', className)} />
      {label}
    </span>
  );
}

function perfTtft(ttft: {avg: number; p50: number} | null): React.ReactNode {
  if (!ttft) return <span className="text-muted-foreground">—</span>;
  return (
    <>
      {fmtMs(ttft.avg)}
      <span className="ml-1 text-[11px] text-muted-foreground">(P50 {fmtMs(ttft.p50)})</span>
    </>
  );
}

function perfSpeed(tps: {avg: number} | null): React.ReactNode {
  if (!tps) return <span className="text-muted-foreground">—</span>;
  return (
    <>
      <span className="font-semibold text-emerald-600 dark:text-emerald-400">
        {tps.avg.toFixed(1)}
      </span>
      <span className="ml-1 text-[11px] text-muted-foreground">tok/s</span>
    </>
  );
}

/** 缓存优先用 Token 口径（cached / prompt），没有再用性能采样的平均值。 */
function cachePct(stat: UsageStat | null, perf: PerfModelStat | null): string {
  if (stat && stat.prompt_tokens > 0) {
    return fmtPct((stat.cached_tokens / stat.prompt_tokens) * 100);
  }
  if (perf?.cache_hit_pct) return fmtPct(perf.cache_hit_pct.avg);
  return '—';
}

function RealmCell({realms, fallback}: {realms: string[]; fallback: string}) {
  if (realms.length > 1) {
    return (
      <span className="inline-flex justify-center gap-1">
        <RealmBadge realm="intl" />
        <RealmBadge realm="cn" />
      </span>
    );
  }
  return <RealmBadge realm={realms[0] || fallback} />;
}

function RealmBadge({realm}: {realm: string}) {
  const isCn = realm === 'cn';
  const isIntl = realm === 'intl';
  return (
    <Badge
      variant="secondary"
      className={cn(
        'px-1.5 py-0 text-[10px] font-normal',
        isCn && 'text-amber-600 dark:text-amber-400',
        isIntl && 'text-blue-600 dark:text-blue-400',
      )}
    >
      {realmLabel(realm)}
    </Badge>
  );
}

function accountsCell(accounts: MatrixAccount[]): React.ReactNode {
  if (!accounts.length) return <span className="text-muted-foreground">—</span>;
  if (accounts.length === 1) return <span className="font-medium">{accounts[0].name}</span>;
  const shown = accounts.slice(0, MAX_ACCOUNT_CHIPS);
  const rest = accounts.length - shown.length;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {shown.map((a) => (
        <span
          key={a.uid}
          className="rounded bg-black/[0.04] px-1.5 py-0.5 text-[10px] dark:bg-white/[0.06]"
        >
          {a.name}
          <span className="ml-1 text-muted-foreground">({a.count})</span>
        </span>
      ))}
      {rest > 0 && <span className="text-[10px] text-muted-foreground">+{rest}</span>}
    </span>
  );
}

function ShareBar({pct}: {pct: number}) {
  return (
    <span className="flex items-center gap-2">
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-black/[0.06] dark:bg-white/[0.08]">
        <span
          className="block h-full rounded-full bg-violet-500"
          style={{width: `${Math.max(3, Math.min(100, pct))}%`}}
        />
      </span>
      <span className="text-[11px] text-muted-foreground tabular-nums">{pct}%</span>
    </span>
  );
}
