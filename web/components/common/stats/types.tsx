/**
 * 「数据指标」页的后端返回结构。
 *
 * 字段名逐项对照 qoder2api/usage.py 核对：
 *   · AnalyticsData  ← compute_usage_analytics()
 *   · PerfData       ← perf_stats()
 *   · UsageSnapshot  ← usage_snapshot()
 *   · ByAccountRow   ← usage_by_account()
 */

export type Scope = 'today' | 'all';

/** 单一口径（今日 / 全部历史）的汇总统计，finalize() 之后还会多出 4 个派生字段。 */
export interface UsageStat {
  requests: number;
  errors: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  cache_hit_pct: number;
  reasoning_ratio: number;
  ttft_ms_avg: number;
  speed_avg: number;
  elapsed_ms_avg: number;
}

/** 账号维度下「某模型用了多少」的一条明细。 */
export interface ModelUseStat {
  requests: number;
  tokens: number;
  reasoning: number;
}

export interface CreditInfo {
  remain?: number;
  size?: number;
  updated_at?: number;
  [k: string]: unknown;
}

export interface AnalyticsAccount {
  uid: string;
  nickname: string;
  realm: string;
  domain: string;
  credits?: CreditInfo | null;
  today: UsageStat;
  all_time: UsageStat;
  today_models: Record<string, ModelUseStat>;
  all_models: Record<string, ModelUseStat>;
}

export interface AnalyticsModel {
  model: string;
  today: UsageStat;
  all_time: UsageStat;
}

export interface AnalyticsData {
  today_ts?: number;
  summary: {today: UsageStat; all_time: UsageStat};
  accounts: AnalyticsAccount[];
  models: AnalyticsModel[];
}

export interface PerfBlock {
  avg: number;
  p50: number;
  p90: number;
  p99: number;
  max: number;
  samples: number;
}

export interface PerfModelStat {
  requests: number;
  errors: number;
  success_rate_pct: number | null;
  ttft_ms: PerfBlock | null;
  generation_ms: PerfBlock | null;
  wall_ms: PerfBlock | null;
  tokens_per_sec: PerfBlock | null;
  cache_hit_pct: PerfBlock | null;
}

export interface PerfData {
  sampled: number;
  success: number;
  errors: number;
  success_rate_pct: number | null;
  ttft_ms: PerfBlock | null;
  generation_ms: PerfBlock | null;
  wall_ms: PerfBlock | null;
  tokens_per_sec: PerfBlock | null;
  cache_hit_pct: PerfBlock | null;
  by_model: Record<string, PerfModelStat>;
}

export interface SnapshotModelStat {
  requests: number;
  accounts?: Record<string, number>;
  [k: string]: unknown;
}

export interface UsageSnapshot {
  requests: number;
  errors: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  reasoning_tokens?: number;
  cached_tokens?: number;
  total_tokens: number;
  by_model?: Record<string, SnapshotModelStat>;
  accounts_map?: Record<string, {nickname?: string; realm?: string}>;
}

export interface ByAccountRow {
  account: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  models: Array<[string, number]>;
}
