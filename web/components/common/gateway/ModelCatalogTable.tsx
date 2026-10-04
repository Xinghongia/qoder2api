'use client';

import * as React from 'react';
import {Boxes, ChevronDown, ChevronUp, Loader2, RefreshCw} from 'lucide-react';

import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
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
 * 「上下文窗口 / 思考档位」可直接改该模型的默认值（存 GET /settings 的
 * model_overrides，走 POST /settings/save）：客户端请求里带了值时仍以
 * 客户端为准，这里只是「没带时的默认」；空值 = 恢复跟随官方默认。
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

/** 面板保存的「每模型默认值」：GET /settings 返回的 model_overrides。 */
interface ModelOverride {
  context_window?: number;
  effort?: string;
}
type ModelOverrides = Record<string, ModelOverride>;
type OverrideField = 'context_window' | 'effort';

/** 「跟随官方默认」下拉哨兵值：Radix Select 不允许空字符串选项，保存时映射回空值。 */
const FOLLOW_DEFAULT = '__follow_official_default__';

/** 生成「写入 / 清除某项覆盖」后的新表（纯函数，供乐观更新与失败回滚使用）。 */
function withOverride(
  overrides: ModelOverrides,
  ovKey: string,
  field: OverrideField,
  value: string,
): ModelOverrides {
  const next: ModelOverrides = {...overrides};
  const item: ModelOverride = {...(next[ovKey] || {})};
  if (field === 'context_window') {
    const n = Number(value);
    if (value && Number.isFinite(n) && n > 0) item.context_window = n;
    else delete item.context_window;
  } else if (value) {
    item.effort = value;
  } else {
    delete item.effort;
  }
  if (item.context_window || item.effort) next[ovKey] = item;
  else delete next[ovKey];
  return next;
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

/**
 * 上下文窗口：有官方多窗口时是「默认值下拉」（空 = 跟随官方默认），
 * 没有多窗口但有 context_length 时保持静态展示。
 */
function renderContext(
  m: ModelEntry,
  override: ModelOverride | undefined,
  saving: boolean,
  onSave: (value: string) => void,
): React.ReactNode {
  const windows = m.context_windows || [];
  if (windows.length) {
    // 官方标签与窗口一一对应才采用，否则统一 K/M 换算。
    const labels =
      m.context_window_labels && m.context_window_labels.length === windows.length
        ? m.context_window_labels
        : null;
    const labelAt = (i: number, w: number) => (labels && labels[i]) || fmtK(w);
    const allLabels = windows.map((w, i) => labelAt(i, w));
    const options = windows.map((w, i) => ({value: String(w), label: labelAt(i, w)}));
    const curWin = override?.context_window != null ? String(override.context_window) : '';
    if (curWin && !options.some((o) => o.value === curWin)) {
      // 当前覆盖值不在官方列表里（如官方窗口表更新过）：插到最前，保留可回显。
      options.unshift({value: curWin, label: fmtK(Number(curWin))});
    }
    const defLabel = m.context_window_default || allLabels[0] || '-';
    return (
      <div className="space-y-1">
        <Select
          value={curWin || FOLLOW_DEFAULT}
          onValueChange={(v) => onSave(v === FOLLOW_DEFAULT ? '' : v)}
          disabled={saving}
        >
          <SelectTrigger
            size="sm"
            className="h-7 w-full max-w-[9rem] rounded-lg text-xs"
            aria-label="默认上下文窗口"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={FOLLOW_DEFAULT}>跟随官方默认</SelectItem>
            {options.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div
          className={cn(
            'text-[10px]',
            curWin ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground',
          )}
        >
          {curWin ? `已自定义（官方默认 ${defLabel}）` : `可选 ${allLabels.join(' / ')}`}
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

/**
 * 思考档位：固定档位只显示徽章；官方给了档位表（或可关闭）时是下拉，
 * 空 = 跟随官方默认；否则官方原生推理只显示「原生」徽章。
 */
function renderEffort(
  m: ModelEntry,
  override: ModelOverride | undefined,
  saving: boolean,
  onSave: (value: string) => void,
): React.ReactNode {
  if (m.reasoning_fixed_effort) {
    return (
      <Badge variant="outline" className={cn('rounded-full text-[10px]', TONE_BADGE.violet)}>
        {m.reasoning_fixed_effort} · 固定
      </Badge>
    );
  }
  const efforts = (m.reasoning_efforts || []).map((e) => String(e));
  if (m.reasoning_can_disable && !efforts.some((e) => e.toLowerCase() === 'none')) {
    efforts.push('none');
  }
  if (efforts.length) {
    const curEff = String(override?.effort || '').trim();
    return (
      <div className="space-y-1">
        <Select
          value={curEff.toLowerCase() || FOLLOW_DEFAULT}
          onValueChange={(v) => onSave(v === FOLLOW_DEFAULT ? '' : v)}
          disabled={saving}
        >
          <SelectTrigger
            size="sm"
            className="h-7 w-full max-w-[9rem] rounded-lg text-xs"
            aria-label="默认思考档位"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={FOLLOW_DEFAULT}>跟随官方默认</SelectItem>
            {efforts.map((e) => (
              <SelectItem key={e} value={e.toLowerCase()}>
                {e}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div
          className={cn(
            'text-[10px]',
            curEff ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground',
          )}
        >
          {curEff ? '已自定义' : `可选 ${efforts.join(' / ')}`}
        </div>
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
  /** GET /settings 的 model_overrides：{<realm>:<key>: {context_window, effort}}。 */
  const [overrides, setOverrides] = React.useState<ModelOverrides>({});
  /** 保存中的下拉：key 为 `<ovKey>|<field>`，期间禁用该下拉。 */
  const [saving, setSaving] = React.useState<Record<string, boolean>>({});
  /** 与 state 同步的镜像：保存失败回滚时要用「保存前」快照。 */
  const overridesRef = React.useRef<ModelOverrides>({});

  const applyOverrides = React.useCallback((next: ModelOverrides) => {
    overridesRef.current = next;
    setOverrides(next);
  }, []);

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

  /**
   * 面板保存的每模型默认值（/settings 是面板接口，必须等鉴权就绪）。
   * 拉取失败不打断模型表：下拉按「无覆盖」显示，不整体报错。
   */
  const loadOverrides = React.useCallback(async () => {
    try {
      const r = (await api.settings.get()) as {model_overrides?: ModelOverrides};
      applyOverrides(r?.model_overrides || {});
    } catch {
      applyOverrides({});
    }
  }, [applyOverrides]);

  useAuthedLoad(() => {
    setExpanded(false); // 切换区域后回到收起态，避免长列表直接铺满
    void load();
    void loadOverrides();
  }, [load, loadOverrides]);

  /**
   * change 即保存（无需确认）：空值 = 恢复跟随官方默认。
   * 成功后用响应里的完整 model_overrides 就地更新，不重拉模型/设置；
   * 失败提示并回滚到保存前的服务端状态。
   */
  const saveOverride = React.useCallback(
    async (ovKey: string, field: OverrideField, value: string) => {
      const saveKey = `${ovKey}|${field}`;
      const prev = overridesRef.current;
      applyOverrides(withOverride(prev, ovKey, field, value)); // 乐观显示
      setSaving((s) => ({...s, [saveKey]: true}));
      try {
        const r = (await api.settings.save({
          model_overrides: {[ovKey]: {[field]: value}},
        })) as {model_overrides?: ModelOverrides};
        if (r?.model_overrides && typeof r.model_overrides === 'object') {
          applyOverrides(r.model_overrides);
        }
        notify.ok(
          value
            ? `已保存默认（${field === 'context_window' ? '窗口' : '档位'} ${value}），客户端未指定时生效`
            : '已恢复跟随官方默认',
        );
      } catch (e) {
        notify.err('保存失败', errText(e));
        applyOverrides(prev);
      } finally {
        setSaving((s) => {
          if (!s[saveKey]) return s;
          const next = {...s};
          delete next[saveKey];
          return next;
        });
      }
    },
    [applyOverrides],
  );

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
          onClick={() => {
            void load();
            void loadOverrides();
          }}
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
              const ovKey = `${realm}:${m.upstream_key || m.id}`;
              const ov = overrides[ovKey];
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
                  <TableCell className="align-top">
                    {renderContext(m, ov, !!saving[`${ovKey}|context_window`], (value) =>
                      void saveOverride(ovKey, 'context_window', value),
                    )}
                  </TableCell>
                  <TableCell className="pr-4 align-top">
                    {renderEffort(m, ov, !!saving[`${ovKey}|effort`], (value) =>
                      void saveOverride(ovKey, 'effort', value),
                    )}
                  </TableCell>
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
