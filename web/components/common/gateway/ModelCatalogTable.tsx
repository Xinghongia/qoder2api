'use client';

import * as React from 'react';
import {Boxes, ChevronDown, ChevronUp, Loader2, RefreshCw} from 'lucide-react';

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
import {notify} from '@/lib/toast';
import {useAuthedLoad} from '@/lib/use-authed-load';
import {cn} from '@/lib/utils';

/**
 * 模型库表格（「网关与运维」页）。
 *
 * 数据来自 GET /v1/models?realm=，字段见 qoder2api/model_entry.py；
 * 展示口径对齐旧看板 dashboard.html 的「当前版本模型库与能力清单」：
 * 峰谷价、上下文窗口、思考档位、能力徽章、下架原因。
 * 长列表默认只展示前 COLLAPSE_LIMIT 条，可展开全部。
 */

const COLLAPSE_LIMIT = 8;

interface ModelOffPeak {
  active?: boolean;
  window_start?: string;
  window_end?: string;
  badge?: string;
  description?: string;
  discount_factor?: number;
}

/** 后端 model_entry() 的有效字段（未给出的字段不展示）。 */
export interface ModelEntry {
  id: string;
  name?: string;
  upstream_key?: string;
  enabled?: boolean;
  description?: string;
  name_local?: string;
  aliases?: string[];
  disabled_reason?: string;
  disabled_message_key?: string;
  context_length?: number;
  context_windows?: number[];
  context_window_labels?: string[];
  context_window_default?: string;
  max_output_tokens?: number;
  reasoning_efforts?: string[];
  reasoning_default_effort?: string;
  reasoning_fixed_effort?: string;
  reasoning_can_disable?: boolean;
  supports_vision?: boolean;
  supports_tool_calls?: boolean;
  supports_reasoning?: boolean;
  vision?: boolean;
  capabilities?: {vision?: boolean; tool_calls?: boolean; reasoning?: boolean};
  price_factor?: number;
  price_factor_peak?: number;
  price_factor_valley?: number;
  original_price_factor?: number;
  off_peak?: ModelOffPeak;
  off_peak_active_now?: boolean;
  is_free?: boolean;
  is_new?: boolean;
  realm?: string;
  realms?: string[];
}

type Tone = 'ok' | 'warning' | 'muted' | 'violet' | 'sky';

const TONE_BADGE: Record<Tone, string> = {
  ok: 'border-emerald-600/30 bg-emerald-600/10 text-emerald-700 dark:text-emerald-400',
  warning: 'border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  muted: 'border-border/60 text-muted-foreground',
  violet: 'border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-400',
  sky: 'border-sky-600/30 bg-sky-600/10 text-sky-700 dark:text-sky-400',
};

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function num(v: number | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

const pf = (v: number) => `${v.toFixed(2)}x`;

/** 与旧看板 fmtK 同口径（1000 进制）。 */
function fmtK(v: number | null | undefined): string {
  if (!v) return '—';
  const k = Math.round(v / 1000);
  return k >= 1000 ? `${k / 1000}M` : `${k}K`;
}

/** 当前是否处于官方低谷时段：优先后端 off_peak_active_now，缺失时按窗口本地复算（跨午夜）。 */
function offPeakActiveNow(m: ModelEntry): boolean {
  if (typeof m.off_peak_active_now === 'boolean') return m.off_peak_active_now;
  const p = m.off_peak;
  if (!p?.window_start || !p?.window_end) return false;
  const toMin = (s: string) => {
    const [h, mi] = String(s)
      .split(':')
      .map((x) => parseInt(x, 10) || 0);
    return h * 60 + mi;
  };
  const now = new Date();
  const cur = now.getHours() * 60 + now.getMinutes();
  const ws = toMin(p.window_start);
  const we = toMin(p.window_end);
  if (ws === we) return true;
  return ws > we ? cur >= ws || cur < we : cur >= ws && cur < we;
}

function renderPrice(m: ModelEntry): React.ReactNode {
  const peak = num(m.price_factor_peak);
  const valley = num(m.price_factor_valley) ?? num(m.price_factor);
  const orig = num(m.original_price_factor);
  const promo = m.off_peak;
  const active = promo ? offPeakActiveNow(m) : false;

  if (promo && valley != null) {
    const hasDiff = peak != null && peak !== valley;
    const win = `${promo.window_start || '—'}-${promo.window_end || '—'}`;
    return (
      <div className="space-y-0.5">
        <Badge
          variant="outline"
          className={cn('rounded-full text-[11px]', active ? TONE_BADGE.ok : TONE_BADGE.muted)}
          title={promo.description || undefined}
        >
          {active
            ? `谷 ${pf(valley)} · 低谷生效中`
            : hasDiff && peak != null
              ? `峰 ${pf(peak)} → 谷 ${pf(valley)}`
              : pf(valley)}
        </Badge>
        <div className="text-[10px] text-muted-foreground">
          {active
            ? `峰 ${peak != null ? pf(peak) : '—'} · 谷时段 ${win}`
            : `低谷自 ${promo.window_start || '—'} 起`}
          {promo.badge ? ` · ${promo.badge}` : ''}
        </div>
      </div>
    );
  }
  if (valley === 0) {
    return (
      <div className="space-y-0.5">
        <Badge variant="outline" className={cn('rounded-full text-[11px]', TONE_BADGE.ok)}>
          限时免费 0.00x
        </Badge>
        {orig != null && orig > 0 && (
          <div className="text-[10px] text-muted-foreground line-through">原 {pf(orig)}</div>
        )}
      </div>
    );
  }
  if (peak != null && valley != null && peak !== valley) {
    return (
      <span className="text-xs tabular-nums text-muted-foreground">
        峰 {pf(peak)} → <span className="text-emerald-600 dark:text-emerald-400">谷 {pf(valley)}</span>
      </span>
    );
  }
  if (valley != null) {
    return <span className="text-xs tabular-nums">{pf(valley)}</span>;
  }
  return <span className="text-xs text-muted-foreground">—</span>;
}

function renderCaps(m: ModelEntry): React.ReactNode {
  const vision =
    m.supports_vision || m.vision || m.capabilities?.vision === true;
  const tools = m.supports_tool_calls || m.capabilities?.tool_calls === true;
  const reasoning =
    m.supports_reasoning ||
    !!m.reasoning_fixed_effort ||
    !!m.reasoning_efforts?.length ||
    m.capabilities?.reasoning === true;
  return (
    <div className="flex flex-wrap items-center gap-1">
      <Badge
        variant="outline"
        className={cn('rounded-full text-[10px]', vision ? TONE_BADGE.ok : TONE_BADGE.muted)}
      >
        {vision ? '视觉' : '文本'}
      </Badge>
      {tools && (
        <Badge variant="outline" className={cn('rounded-full text-[10px]', TONE_BADGE.ok)}>
          工具
        </Badge>
      )}
      {reasoning && (
        <Badge variant="outline" className={cn('rounded-full text-[10px]', TONE_BADGE.violet)}>
          推理
        </Badge>
      )}
      {m.is_new && (
        <Badge variant="outline" className={cn('rounded-full text-[10px]', TONE_BADGE.warning)}>
          NEW
        </Badge>
      )}
    </div>
  );
}

function renderContext(m: ModelEntry): React.ReactNode {
  const windows = m.context_windows || [];
  if (windows.length) {
    const labels =
      m.context_window_labels && m.context_window_labels.length === windows.length
        ? m.context_window_labels
        : windows.map((w) => fmtK(w));
    return (
      <div className="space-y-0.5">
        <div className="text-xs tabular-nums">{labels.join(' / ')}</div>
        <div className="text-[10px] text-muted-foreground">
          {m.context_window_default ? `默认 ${m.context_window_default}` : '可选多窗口'}
        </div>
      </div>
    );
  }
  if (m.context_length) {
    return (
      <div className="space-y-0.5">
        <div className="text-xs tabular-nums">{fmtK(m.context_length)}</div>
        <div className="text-[10px] text-muted-foreground">官方单窗口</div>
      </div>
    );
  }
  return <span className="text-xs text-muted-foreground">—</span>;
}

function renderEffort(m: ModelEntry): React.ReactNode {
  if (m.reasoning_fixed_effort) {
    return (
      <Badge variant="outline" className={cn('rounded-full text-[10px]', TONE_BADGE.violet)}>
        {m.reasoning_fixed_effort} · 固定
      </Badge>
    );
  }
  const efforts = (m.reasoning_efforts || []).slice();
  if (m.reasoning_can_disable && !efforts.includes('none')) efforts.push('none');
  if (efforts.length) {
    const def = String(m.reasoning_default_effort || '').toLowerCase();
    return (
      <div className="flex flex-wrap items-center gap-1">
        {efforts.map((e) => (
          <Badge
            key={e}
            variant="outline"
            className={cn(
              'rounded-full text-[10px]',
              String(e).toLowerCase() === def && def ? TONE_BADGE.violet : TONE_BADGE.muted,
            )}
            title={String(e).toLowerCase() === def && def ? '官方默认档位' : undefined}
          >
            {e}
            {String(e).toLowerCase() === def && def ? ' ·默认' : ''}
          </Badge>
        ))}
      </div>
    );
  }
  if (m.supports_reasoning || m.capabilities?.reasoning === true) {
    return (
      <Badge variant="outline" className={cn('rounded-full text-[10px]', TONE_BADGE.violet)}>
        原生
      </Badge>
    );
  }
  return <span className="text-xs text-muted-foreground">—</span>;
}

export function ModelCatalogTable({realm}: {realm: 'intl' | 'cn'}) {
  const [models, setModels] = React.useState<ModelEntry[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [expanded, setExpanded] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const r = (await api.models(realm)) as {data?: ModelEntry[]};
      setModels(r?.data || []);
    } catch (e) {
      notify.err('模型库加载失败', errText(e));
      setModels([]);
    } finally {
      setLoading(false);
    }
  }, [realm]);

  useAuthedLoad(() => {
    setExpanded(false); // 切换区域后回到收起态，避免长列表直接铺满
    void load();
  }, [load]);

  const openCount = models.filter((m) => m.enabled !== false).length;
  const disabledCount = models.length - openCount;
  const visible = expanded ? models : models.slice(0, COLLAPSE_LIMIT);

  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-2 pt-3">
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <Boxes className="size-3.5" />
          <span>
            模型库（{realm === 'cn' ? '国内版' : '国际版'}）· 共 {models.length} 个 · 开放{' '}
            {openCount} 个
            {disabledCount > 0 ? ` · 下架 ${disabledCount} 个` : ''}
          </span>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 rounded-full text-xs"
          disabled={loading}
          onClick={() => void load()}
        >
          {loading ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <RefreshCw className="size-3.5" />
          )}
          刷新
        </Button>
      </div>

      <Table>
        <TableHeader>
          <TableRow className="border-b border-border/60 hover:bg-transparent">
            <TableHead className="pl-4 text-[11px] font-normal text-muted-foreground">
              模型
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              价格（峰 / 谷）
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              能力
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              上下文窗口
            </TableHead>
            <TableHead className="pr-4 text-[11px] font-normal text-muted-foreground">
              思考档位
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {loading && models.length === 0 ? (
            Array.from({length: 4}).map((_, i) => (
              <TableRow key={i} className="border-b border-border/40 hover:bg-transparent">
                <TableCell colSpan={5} className="pl-4">
                  <Skeleton className="h-5 w-full" />
                </TableCell>
              </TableRow>
            ))
          ) : visible.length === 0 ? (
            <TableRow className="border-0 hover:bg-transparent">
              <TableCell colSpan={5} className="py-10 text-center text-xs text-muted-foreground">
                暂无模型数据
              </TableCell>
            </TableRow>
          ) : (
            visible.map((m) => {
              const disabled = m.enabled === false;
              return (
                <TableRow
                  key={m.upstream_key || m.id}
                  className={cn('border-b border-border/40', disabled && 'opacity-60')}
                >
                  <TableCell className="pl-4 align-top">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-sm font-medium">{m.id}</span>
                      {disabled && (
                        <Badge
                          variant="outline"
                          className={cn('rounded-full text-[10px]', TONE_BADGE.warning)}
                          title={m.disabled_reason || '需要升级或购买千问官方套餐开放'}
                        >
                          已下架
                        </Badge>
                      )}
                    </div>
                    {(m.upstream_key && m.upstream_key !== m.id) || m.name_local ? (
                      <div className="mt-0.5 text-[10px] text-muted-foreground">
                        {m.upstream_key && m.upstream_key !== m.id ? (
                          <span className="font-mono">key: {m.upstream_key}</span>
                        ) : null}
                        {m.name_local ? (
                          <span className={m.upstream_key && m.upstream_key !== m.id ? 'ml-2' : ''}>
                            官方别名: {m.name_local}
                          </span>
                        ) : null}
                      </div>
                    ) : null}
                    {disabled && m.disabled_reason ? (
                      <div className="mt-0.5 max-w-[420px] text-[10px] text-amber-600 dark:text-amber-400">
                        {m.disabled_reason}
                      </div>
                    ) : null}
                    {m.description ? (
                      <div
                        className="mt-0.5 line-clamp-2 max-w-[420px] text-[11px] leading-4 text-muted-foreground"
                        title={m.description}
                      >
                        {m.description}
                      </div>
                    ) : null}
                  </TableCell>
                  <TableCell className="align-top">{renderPrice(m)}</TableCell>
                  <TableCell className="align-top">{renderCaps(m)}</TableCell>
                  <TableCell className="align-top">{renderContext(m)}</TableCell>
                  <TableCell className="pr-4 align-top">{renderEffort(m)}</TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>

      {models.length > COLLAPSE_LIMIT && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex w-full items-center justify-center gap-1 border-t border-border/40 py-2 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
        >
          {expanded ? (
            <>
              <ChevronUp className="size-3.5" />
              收起（只显示前 {COLLAPSE_LIMIT} 个）
            </>
          ) : (
            <>
              <ChevronDown className="size-3.5" />
              展开全部 {models.length} 个
            </>
          )}
        </button>
      )}
    </section>
  );
}
