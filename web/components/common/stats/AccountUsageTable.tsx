'use client';

import * as React from 'react';
import {Users} from 'lucide-react';

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
import {fmt, NUM, realmLabel, scopeLabel} from './format';
import type {
  AnalyticsAccount,
  ByAccountRow,
  CreditInfo,
  ModelUseStat,
  Scope,
  UsageStat,
} from './types';

interface AccountRow {
  uid: string;
  nickname: string;
  realm: string;
  credits: CreditInfo | null;
  /** 当前口径（今日 / 全部）的统计；仅日志里存在的账号为 null。 */
  stat: UsageStat | null;
  /** 当前口径下按 Token 倒序的模型明细。 */
  models: Array<[string, ModelUseStat]>;
  /** /usage/by-account 的原始口径行，仅用于账号池已移除、日志仍有记录的账号。 */
  raw: ByAccountRow | null;
}

/**
 * 各账号用量透视与各模型消耗明细（对齐旧看板 renderAnalytics 的账号表）。
 *
 * 主数据来自 /usage/analytics.accounts（带昵称 / 区域 / 积分，跟随今日-全部切换）；
 * /usage/by-account 直读日志，用来补上账号池里已不存在、但日志有记录的账号。
 */
export function AccountUsageTable({
  accounts,
  byAccount,
  scope,
  loading,
}: {
  accounts: AnalyticsAccount[];
  byAccount: ByAccountRow[];
  scope: Scope;
  loading: boolean;
}) {
  const rows = React.useMemo<AccountRow[]>(() => {
    const seen = new Set<string>();
    const out: AccountRow[] = [];

    for (const a of accounts) {
      if (!a?.uid || seen.has(a.uid)) continue;
      const idle =
        !(a.today?.requests || a.today?.errors || a.all_time?.requests || a.all_time?.errors);
      if (a.uid === '(unattributed)' && idle) continue;
      seen.add(a.uid);
      const stat = (scope === 'today' ? a.today : a.all_time) ?? null;
      const map = (scope === 'today' ? a.today_models : a.all_models) ?? {};
      const models = Object.entries(map)
        .filter(([, ms]) => (ms?.requests ?? 0) > 0)
        .sort((x, y) => (y[1].tokens || 0) - (x[1].tokens || 0)) as Array<
        [string, ModelUseStat]
      >;
      out.push({
        uid: a.uid,
        nickname: a.nickname || a.uid,
        realm: a.realm || '',
        credits: a.credits ?? null,
        stat,
        models,
        raw: null,
      });
    }

    for (const b of byAccount) {
      if (!b?.account || seen.has(b.account)) continue;
      seen.add(b.account);
      out.push({
        uid: b.account,
        nickname: b.account,
        realm: '',
        credits: null,
        stat: null,
        models: [],
        raw: b,
      });
    }

    return out.sort(
      (x, y) =>
        (y.stat?.total_tokens ?? y.raw?.total_tokens ?? 0) -
        (x.stat?.total_tokens ?? x.raw?.total_tokens ?? 0),
    );
  }, [accounts, byAccount, scope]);

  if (loading && !accounts.length && !byAccount.length) {
    return <Skeleton className="h-56 w-full rounded-[20px]" />;
  }

  return (
    <section className="rounded-[20px] bg-muted p-3 md:p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2 px-1">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <Users className="size-4 text-muted-foreground" />
          各账号用量透视与各模型消耗明细
          {rows.length > 0 && (
            <span className="text-xs font-normal text-muted-foreground">
              （{rows.length} 个账号）
            </span>
          )}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          请求与 Token 口径：{scopeLabel(scope)}；模型明细按 Token 倒序。
        </p>
      </div>

      {rows.length === 0 ? (
        <EmptyState
          icon={Users}
          title="暂无账号用量数据"
          description="账号池为空，或还没有产生任何调用记录。"
          className="flex flex-col items-center justify-center p-8 text-center"
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow className="border-border/40 hover:bg-transparent">
              <TableHead className="text-[11px] text-muted-foreground">账号</TableHead>
              <TableHead className="w-[84px] text-center text-[11px] text-muted-foreground">区域</TableHead>
              <TableHead className="w-[150px] text-[11px] text-muted-foreground">积分</TableHead>
              <TableHead className="text-right text-[11px] text-muted-foreground">请求数</TableHead>
              <TableHead className="text-right text-[11px] text-muted-foreground">总 Token</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">各模型用量</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => {
              const requests = row.stat?.requests ?? row.raw?.requests ?? 0;
              const tokens = row.stat?.total_tokens ?? row.raw?.total_tokens ?? 0;
              return (
                <TableRow key={row.uid} className="border-border/40">
                  <TableCell>
                    <div className="font-medium">{row.nickname}</div>
                    <div className="font-mono text-[11px] text-muted-foreground">
                      {row.uid.slice(0, 10)}
                      {row.uid.length > 10 ? '…' : ''}
                    </div>
                  </TableCell>
                  <TableCell className="text-center">
                    {row.realm ? (
                      <Badge
                        variant="secondary"
                        className={cn(
                          'px-1.5 py-0 text-[10px] font-normal',
                          row.realm === 'cn'
                            ? 'text-amber-600 dark:text-amber-400'
                            : 'text-blue-600 dark:text-blue-400',
                        )}
                      >
                        {realmLabel(row.realm)}
                      </Badge>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <CreditCell credits={row.credits} />
                  </TableCell>
                  <TableCell className={cn('text-right', NUM)}>{fmt(requests)}</TableCell>
                  <TableCell className={cn('text-right font-semibold', NUM)}>{fmt(tokens)}</TableCell>
                  <TableCell className="max-w-[440px] whitespace-normal">
                    <ModelPills row={row} />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

function CreditCell({credits}: {credits: CreditInfo | null}) {
  if (credits?.remain == null) return <span className="text-muted-foreground">—</span>;
  return (
    <>
      <span className={cn('font-semibold text-emerald-600 dark:text-emerald-400', NUM)}>
        {fmt(credits.remain)}
      </span>
      <span className="ml-1 text-[11px] text-muted-foreground">
        {credits.size ? `/ ${fmt(credits.size)} 积分` : '积分'}
      </span>
    </>
  );
}

function ModelPills({row}: {row: AccountRow}) {
  if (row.models.length) {
    return (
      <span className="inline-flex flex-wrap gap-1">
        {row.models.map(([model, ms]) => (
          <span
            key={model}
            className="rounded bg-black/[0.04] px-1.5 py-0.5 text-[10px] dark:bg-white/[0.06]"
          >
            <span className="font-medium">{model}</span>
            {`: ${ms.requests} 次 · `}
            <span className="font-semibold text-violet-600 dark:text-violet-400">
              {fmt(ms.tokens)}
            </span>
            {' tok'}
          </span>
        ))}
      </span>
    );
  }
  if (row.raw?.models?.length) {
    return (
      <span className="inline-flex flex-wrap items-center gap-1">
        {row.raw.models.map(([model, count]) => (
          <span
            key={model}
            className="rounded bg-black/[0.04] px-1.5 py-0.5 text-[10px] dark:bg-white/[0.06]"
          >
            <span className="font-medium">{model}</span>
            {`: ${count} 次`}
          </span>
        ))}
        <span className="text-[10px] text-muted-foreground">（仅全部历史日志）</span>
      </span>
    );
  }
  return <span className="text-muted-foreground">无调用</span>;
}
