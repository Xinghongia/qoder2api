'use client';

import {Activity, Coins, Database, Gauge, Percent} from 'lucide-react';

import {StatCard} from '@/components/common/layout/StatCard';
import {Skeleton} from '@/components/ui/skeleton';
import {fmt, fmtAvgSeconds, fmtMs, fmtPct, scopeLabel} from './format';
import type {Scope, UsageStat} from './types';

/** 后端还没返回或字段缺失时的零值，保证卡片始终有稳定结构。 */
const ZERO: UsageStat = {
  requests: 0,
  errors: 0,
  prompt_tokens: 0,
  completion_tokens: 0,
  reasoning_tokens: 0,
  cached_tokens: 0,
  total_tokens: 0,
  cache_hit_pct: 0,
  reasoning_ratio: 0,
  ttft_ms_avg: 0,
  speed_avg: 0,
  elapsed_ms_avg: 0,
};

/**
 * 顶层 KPI 看板（对齐旧看板 #analyticsKpiCards 的 5 张卡）：
 * 前两张固定展示「今日 / 累计」消耗，后三张跟随今日-全部切换。
 */
export function UsageKpi({
  today,
  allTime,
  scope,
  loading,
}: {
  today: UsageStat | null;
  allTime: UsageStat | null;
  scope: Scope;
  loading: boolean;
}) {
  if (loading && !today && !allTime) {
    return (
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 md:gap-4 lg:grid-cols-5">
        {Array.from({length: 5}).map((_, i) => (
          <Skeleton key={i} className="h-[96px] rounded-[20px]" />
        ))}
      </div>
    );
  }

  const t = today ?? ZERO;
  const a = allTime ?? ZERO;
  const cur = scope === 'today' ? t : a;
  const totalReq = (cur.requests || 0) + (cur.errors || 0);
  const successPct = totalReq > 0 ? (cur.requests / totalReq) * 100 : 100;

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 md:gap-4 lg:grid-cols-5">
      <StatCard
        label="今日消耗 Token"
        value={fmt(t.total_tokens)}
        hint={`输入 ${fmt(t.prompt_tokens)} · 输出 ${fmt(t.completion_tokens)}`}
        icon={Coins}
        tone="info"
      />
      <StatCard
        label="累计消耗 Token"
        value={fmt(a.total_tokens)}
        hint={`输入 ${fmt(a.prompt_tokens)} · 输出 ${fmt(a.completion_tokens)}`}
        icon={Database}
        tone="accent"
        delay={0.04}
      />
      <StatCard
        label="网关平均生成速度"
        value={
          <>
            {(cur.speed_avg || 0).toFixed(1)}
            <span className="ml-1 text-xs font-normal text-gray-500 dark:text-gray-400">tok/s</span>
          </>
        }
        hint={`平均首字延迟 ${cur.ttft_ms_avg ? fmtMs(cur.ttft_ms_avg) : '—'} · 耗时 ${fmtAvgSeconds(cur.elapsed_ms_avg)}`}
        icon={Gauge}
        tone="success"
        delay={0.08}
      />
      <StatCard
        label="上下文缓存命中率"
        value={fmtPct(cur.cache_hit_pct)}
        hint="提示词前缀命中复用比"
        icon={Percent}
        tone={cur.cache_hit_pct > 0 ? 'info' : 'neutral'}
        delay={0.12}
      />
      <StatCard
        label={`${scopeLabel(scope)}请求数 & 成功率`}
        value={
          <>
            {fmt(cur.requests)}
            <span className="ml-1 text-xs font-normal text-gray-500 dark:text-gray-400">次</span>
          </>
        }
        hint={`成功率 ${successPct.toFixed(1)}% · 失败 ${fmt(cur.errors)}`}
        icon={Activity}
        tone={cur.errors > 0 ? 'warning' : 'success'}
        delay={0.16}
      />
    </div>
  );
}
