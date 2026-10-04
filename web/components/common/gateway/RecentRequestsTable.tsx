'use client';

import * as React from 'react';

import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Skeleton} from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {api} from '@/lib/api';
import type {Realm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';
import {useAuthedLoad} from '@/lib/use-authed-load';
import {cn} from '@/lib/utils';

/**
 * 「最近请求」表格（「网关与运维」页）。
 *
 * 行为基准是旧看板 dashboard.html 的最近请求区：
 *   · 每页条数 10/20/50/100，沿用旧键名 WB_RECENT_LIMIT（老用户偏好不丢），
 *     非法值回退 20，切换后回到第 1 页；
 *   · 页码列表算法、上一页 / 下一页禁用边界、`第 X / Y 页 · 共 N 条记录`
 *     与旧版一致（total ≤ 7 全列，其余带省略号）；
 *   · 表头 13 列：时间 / 模型 / 账号 / 模式 / 耗时 / 首字 / 速度 / 输入 /
 *     输出 / 思考 / 缓存 / 总 token / 积分（每次请求实际消耗，上游
 *     usage.credits；0 = 本次未计费）；失败行在耗时列显示红色「失败」徽章；
 *   · 切换区域回到第 1 页并重拉；加载失败只提示、不清空已展示的数据。
 */

const LIMIT_OPTIONS = [10, 20, 50, 100] as const;
const DEFAULT_LIMIT = 20;
const LIMIT_STORE = 'WB_RECENT_LIMIT';
const COLUMN_COUNT = 13;

/** 后端 recent_usage()（qoder2api/usage.py）返回的单行；失败行没有 stream / 用量字段。 */
interface RecentRow {
  iso?: string;
  model?: string;
  account?: string;
  /** 账号昵称（后端按 uid 现查账号池；账号已删时为空 -> 回落 uid 前缀）。 */
  account_name?: string;
  stream?: boolean;
  error?: boolean;
  status?: number;
  message?: string;
  elapsed_ms?: number | null;
  ttft_ms?: number | null;
  gen_ms?: number | null;
  tokens_per_sec?: number | null;
  prompt_tokens?: number;
  completion_tokens?: number;
  reasoning_tokens?: number;
  cached_tokens?: number;
  cache_hit_pct?: number | null;
  total_tokens?: number;
  /** 本次请求实际消耗积分（上游 usage.credits；免费模型/失败行为 0/缺失）。 */
  credit?: number;
  realm?: string;
}

interface RecentPayload {
  total?: number;
  page?: number;
  limit?: number;
  total_pages?: number;
  rows?: RecentRow[];
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

const fmt = (n: number | null | undefined) => (n ?? 0).toLocaleString('en-US');

/** 积分：上游 credits 是小数（如 0.0192），0 直接写 0，非零最多保留 4 位。 */
const fmtCredit = (n: number | null | undefined) => {
  const v = n ?? 0;
  if (!Number.isFinite(v) || v === 0) return '0';
  return v.toFixed(4).replace(/\.?0+$/, '');
};

const TONE = {
  danger: 'border-red-500/35 bg-red-500/10 text-red-700 dark:text-red-400',
  ok: 'border-emerald-600/30 bg-emerald-600/10 text-emerald-700 dark:text-emerald-400',
  muted: 'border-border/60 text-muted-foreground',
};

/** 页码列表：与旧看板 generatePageList 同算法（total ≤ 7 时全部列出）。 */
function pageList(current: number, total: number): Array<number | '...'> {
  if (total <= 7) {
    return Array.from({length: total}, (_, i) => i + 1);
  }
  const pages: Array<number | '...'> = [1];
  let start = Math.max(2, current - 2);
  let end = Math.min(total - 1, current + 2);
  if (current <= 4) {
    end = 5;
  }
  if (current >= total - 3) {
    start = total - 4;
  }
  if (start > 2) pages.push('...');
  for (let i = start; i <= end; i++) {
    pages.push(i);
  }
  if (end < total - 1) pages.push('...');
  pages.push(total);
  return pages;
}

export function RecentRequestsTable({
  realm,
  title = '最近请求',
  hint,
  from,
  to,
}: {
  realm: Realm;
  /** 卡片标题：仪表盘用默认「最近请求」；用量页用「全部请求记录」。 */
  title?: string;
  /** 标题旁的小字说明（如「仅显示最新 100 条」）。 */
  hint?: string;
  /** 本地日期范围（YYYY-MM-DD，含两端）；不传 = 不限时间。 */
  from?: string;
  to?: string;
}) {
  // limit 为 null 表示「本地偏好还没读出来」：先不发请求，读出来后只发一次。
  const [limit, setLimit] = React.useState<number | null>(null);
  const [page, setPage] = React.useState(1);
  const [total, setTotal] = React.useState(0);
  const [totalPages, setTotalPages] = React.useState(1);
  const [rows, setRows] = React.useState<RecentRow[]>([]);
  const [loading, setLoading] = React.useState(true);

  // 每页条数偏好沿用旧看板的 WB_RECENT_LIMIT；SSR / 首帧先按默认 20 渲染，挂载后校正。
  React.useEffect(() => {
    let value = DEFAULT_LIMIT;
    try {
      const raw = parseInt(localStorage.getItem(LIMIT_STORE) || String(DEFAULT_LIMIT), 10);
      if ((LIMIT_OPTIONS as readonly number[]).includes(raw)) value = raw;
    } catch {
      value = DEFAULT_LIMIT;
    }
    setLimit(value);
  }, []);

  // 区域 / 时间范围切换回到第 1 页：渲染期同步校正，避免先用旧页码对新
  // 筛选条件发一次请求（用量页的日期选择一变，页码必须跟着归零）。
  const rangeKey = `${realm}|${from || ''}|${to || ''}`;
  const [pageKey, setPageKey] = React.useState(rangeKey);
  if (pageKey !== rangeKey) {
    setPageKey(rangeKey);
    setPage(1);
  }

  const load = React.useCallback(async () => {
    if (limit === null) return;
    setLoading(true);
    try {
      const r = (await api.usage.recent(limit, page, realm, from, to)) as RecentPayload;
      const nextTotal = r?.total || 0;
      const nextTotalPages =
        r?.total_pages || Math.max(1, Math.ceil(nextTotal / limit));
      const serverPage = r?.page || 1;
      setTotal(nextTotal);
      setTotalPages(nextTotalPages);
      setRows(r?.rows || []);
      // 后端会把越界页码夹回有效范围，以响应里的 page 为准
      if (serverPage !== page) setPage(serverPage);
    } catch (e) {
      notify.err('最近请求加载失败', errText(e)); // 保留已有数据，不清空
    } finally {
      setLoading(false);
    }
  }, [limit, page, realm, from, to]);

  useAuthedLoad(() => {
    void load();
  }, [load]);

  const gotoPage = (p: number) => {
    if (p < 1 || p > totalPages || p === page) return;
    setPage(p);
  };

  const pickLimit = (n: number) => {
    if (n === limit) return;
    setLimit(n);
    setPage(1);
    try {
      localStorage.setItem(LIMIT_STORE, String(n));
    } catch {
      /* 隐私模式下 localStorage 不可用，忽略 */
    }
  };

  const showSkeleton = loading && rows.length === 0;

  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-2 pt-3">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="text-sm font-medium">{title}</h2>
          {hint && <span className="text-[11px] text-muted-foreground">{hint}</span>}
          <span className="text-[11px] text-muted-foreground">
            (第 {page} 页 / 共 {fmt(total)} 条)
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-muted-foreground">显示条数:</span>
          <div className="flex items-center gap-0.5 rounded-full border border-border/60 bg-background/60 p-0.5">
            {LIMIT_OPTIONS.map((n) => (
              <Button
                key={n}
                variant={limit === n ? 'secondary' : 'ghost'}
                size="sm"
                className="h-6 min-w-[30px] rounded-full px-2 text-[11px] tabular-nums"
                onClick={() => pickLimit(n)}
              >
                {n}
              </Button>
            ))}
          </div>
        </div>
      </div>

      <Table>
        <TableHeader>
          <TableRow className="border-b border-border/60 hover:bg-transparent">
            <TableHead className="pl-4 text-[11px] font-normal text-muted-foreground">
              时间
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              模型
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              账号
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              模式
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              耗时
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              首字
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              速度
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              输入
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              输出
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              思考
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              缓存
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              总 token
            </TableHead>
            <TableHead className="pr-4 text-[11px] font-normal text-muted-foreground">
              积分
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {showSkeleton ? (
            Array.from({length: 5}).map((_, i) => (
              <TableRow key={i} className="border-b border-border/40 hover:bg-transparent">
                <TableCell colSpan={COLUMN_COUNT} className="pl-4">
                  <Skeleton className="h-5 w-full" />
                </TableCell>
              </TableRow>
            ))
          ) : rows.length === 0 ? (
            <TableRow className="border-0 hover:bg-transparent">
              <TableCell
                colSpan={COLUMN_COUNT}
                className="py-10 text-center text-xs text-muted-foreground"
              >
                还没有请求记录
              </TableCell>
            </TableRow>
          ) : (
            rows.map((r, i) => {
              const stream = r.stream === true;
              return (
                <TableRow key={`${r.iso || ''}-${i}`} className="border-b border-border/40">
                  <TableCell className="pl-4 font-mono text-xs tabular-nums text-muted-foreground">
                    {(r.iso || '').replace('T', ' ').slice(5)}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{r.model || '—'}</TableCell>
                  {/* 账号显示昵称（如 aliyun3780958258），比 uid 前缀好认；
                      账号已删时后端拿不到名字，回落 uid 前缀。 */}
                  <TableCell className="text-[11px] text-muted-foreground">
                    <span title={r.account || ''}>
                      {r.account_name || (r.account || '—').slice(0, 8)}
                    </span>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={cn('rounded-full text-[10px]', stream ? TONE.ok : TONE.muted)}
                    >
                      {stream ? '流式' : '非流式'}
                    </Badge>
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {r.error ? (
                      <Badge
                        variant="outline"
                        className={cn('rounded-full text-[10px]', TONE.danger)}
                      >
                        失败
                      </Badge>
                    ) : r.elapsed_ms != null ? (
                      `${fmt(r.elapsed_ms)} ms`
                    ) : (
                      '—'
                    )}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {r.ttft_ms != null ? `${fmt(r.ttft_ms)} ms` : '—'}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {r.tokens_per_sec ? r.tokens_per_sec.toFixed(1) : '—'}
                  </TableCell>
                  <TableCell className="tabular-nums">{fmt(r.prompt_tokens)}</TableCell>
                  <TableCell className="tabular-nums">{fmt(r.completion_tokens)}</TableCell>
                  <TableCell className="tabular-nums text-violet-600 dark:text-violet-400">
                    {fmt(r.reasoning_tokens)}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {r.cache_hit_pct != null ? `${r.cache_hit_pct}%` : '—'}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    <b>{fmt(r.total_tokens)}</b>
                  </TableCell>
                  <TableCell className="pr-4 tabular-nums">
                    {r.error ? (
                      '—'
                    ) : (
                      <span
                        className={cn(
                          (r.credit ?? 0) > 0
                            ? 'text-amber-600 dark:text-amber-400'
                            : 'text-muted-foreground',
                        )}
                      >
                        {fmtCredit(r.credit)}
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/40 px-4 py-3 text-[12px] text-muted-foreground">
        <div>
          第 {page} / {totalPages} 页 · 共 {fmt(total)} 条记录
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            className="h-6 rounded-full px-2 text-[11px]"
            disabled={page <= 1}
            onClick={() => gotoPage(page - 1)}
          >
            上一页
          </Button>
          <div className="flex items-center gap-0.5">
            {pageList(page, totalPages).map((p, i) =>
              p === '...' ? (
                <span key={`ellipsis-${i}`} className="px-[3px] text-muted-foreground">
                  …
                </span>
              ) : (
                <Button
                  key={p}
                  variant={p === page ? 'secondary' : 'ghost'}
                  size="sm"
                  className="h-6 min-w-[26px] rounded-full px-1.5 text-[11px] tabular-nums"
                  onClick={() => gotoPage(p)}
                >
                  {p}
                </Button>
              ),
            )}
          </div>
          <Button
            variant="outline"
            size="sm"
            className="h-6 rounded-full px-2 text-[11px]"
            disabled={page >= totalPages}
            onClick={() => gotoPage(page + 1)}
          >
            下一页
          </Button>
        </div>
      </div>
    </section>
  );
}
