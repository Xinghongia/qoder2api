/**
 * 「数据指标」页共用的格式化与文案（与旧看板 dashboard.html 的
 * fmt / ms / pct 同口径，避免新老两版数字看起来不一样）。
 */

import type {Scope} from './types';

/** 数字统一走等宽数字 + 紧字距，和 StatCard 的值保持一致。 */
export const NUM = 'tabular-nums tracking-[-0.03em]';

/** 千分位整数；空值按 0 处理（旧看板 `const fmt = n => (n ?? 0).toLocaleString('en-US')`）。 */
export function fmt(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString('en-US');
}

export function fmtPct(n: number | null | undefined, digits = 1): string {
  return `${(n ?? 0).toFixed(digits)}%`;
}

/** 毫秒；≥1000ms 折算成秒（旧看板 ms()）：null → —，0 → 0 ms。 */
export function fmtMs(v: number | null | undefined): string {
  if (v == null) return '—';
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
}

/** 平均耗时统一显示成秒；0 / 空值显示 —（旧看板 guard 了 falsy）。 */
export function fmtAvgSeconds(ms: number | null | undefined): string {
  return ms ? `${(ms / 1000).toFixed(2)} s` : '—';
}

export function realmLabel(realm: string | undefined | null): string {
  if (realm === 'cn') return '国内版';
  if (realm === 'intl') return '国际版';
  return '未知区域';
}

export function scopeLabel(scope: Scope): string {
  return scope === 'today' ? '今日' : '累计';
}
