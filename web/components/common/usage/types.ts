/**
 * 用量统计页（/usage）的数据契约 —— 后端 qoder2api/usagedb.py 的 stats() 返回。
 *
 * 时间范围：today（今天，按小时）/ 7d / 30d / custom（起止日期）；
 * 维度：汇总 KPI + 按模型 + 按密钥 + 趋势（可按总量/模型/密钥分组）。
 */
export type UsageRangeKey = 'today' | '7d' | '30d' | 'custom';
export type UsageGroup = 'total' | 'model' | 'key_id';
export type UsageMetric = 'tokens' | 'requests';

export interface UsageSummary {
  requests: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  credit: number;
  cache_hit_pct: number;
}

export interface UsageModelRow extends UsageSummary {
  model: string;
}

export interface UsageKeyRow extends UsageSummary {
  key_id: string;
  key_name: string;
}

export interface UsageSeries {
  /** 稳定键：模型名 / Key id / total（图例与 React key 用它）。 */
  name: string;
  /** 展示名：Key 显示名字、未绑定时为「未绑定 Key」。 */
  title: string;
  requests: number[];
  tokens: number[];
  failed: number[];
}

export interface UsageTrend {
  granularity: 'hour' | 'day';
  group: UsageGroup;
  labels: string[];
  series: UsageSeries[];
}

export interface UsageStats {
  ok: boolean;
  range: {
    from: string;
    to: string;
    days: number;
    granularity: 'hour' | 'day';
    group: UsageGroup;
    realm: string;
  };
  summary: UsageSummary;
  by_model: UsageModelRow[];
  by_key: UsageKeyRow[];
  trend: UsageTrend;
}

/** Key 展示名：没有 key_name（历史行/未绑定）时给一个可读的兜底。 */
export function keyTitle(row: {key_id?: string; key_name?: string}): string {
  if (row.key_name) return row.key_name;
  return row.key_id ? row.key_id : '未绑定 Key';
}

/** 紧凑数字（坐标轴刻度 / 图例小字）：1234 → 1.2k，1.2e6 → 1.2M。 */
export function fmtCompact(n: number | null | undefined): string {
  const v = n ?? 0;
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
  return String(v);
}

/**
 * 图表配色：与 workbuddy 管理面板同一套设计令牌（zinc 主题的 chart-1..5），
 * 多序列按序取用；超出 5 条时循环（此时序列已被后端折叠成 <= 8 条）。
 */
export const CHART_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
];

export function seriesColor(i: number): string {
  return CHART_COLORS[i % CHART_COLORS.length];
}
