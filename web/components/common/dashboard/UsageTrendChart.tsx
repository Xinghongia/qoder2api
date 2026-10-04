'use client';

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

export interface DailyPoint {
  date: string;
  day: string;
  requests: number;
  tokens: number;
  failed: number;
  credit: number;
  /** 今日 KPI 用：缓存命中率与平均速度的分母/分子 */
  prompt_tokens: number;
  cached_tokens: number;
  ttft_sum: number;
  ttft_n: number;
  speed_sum: number;
  speed_n: number;
}

/**
 * 14 天用量趋势（仪表盘）：请求数实线面积 + 失败数红色虚线。
 * 结构与配色对齐 workbuddy-manager 的 dashboard 图表（zinc 变量 + chart-1）。
 */
export function UsageTrendChart({data}: {data: DailyPoint[]}) {
  const total = data.reduce((sum, d) => sum + d.requests, 0);
  if (total === 0) {
    return (
      <div className="grid h-[220px] w-full place-items-center text-xs text-muted-foreground">
        近 {data.length} 天还没有请求记录
      </div>
    );
  }
  return (
    <div className="h-[220px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{top: 4, right: 8, bottom: 0, left: -16}}>
          <defs>
            <linearGradient id="gReq" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.35} />
              <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
          <XAxis
            dataKey="day"
            tickLine={false}
            axisLine={false}
            fontSize={11}
            stroke="var(--muted-foreground)"
          />
          <YAxis
            tickLine={false}
            axisLine={false}
            fontSize={11}
            stroke="var(--muted-foreground)"
            allowDecimals={false}
          />
          <Tooltip
            contentStyle={{
              background: 'var(--popover)',
              border: '1px solid var(--border)',
              borderRadius: 12,
              fontSize: 12,
            }}
            labelFormatter={(label) => `日期 ${label}`}
          />
          <Area
            type="monotone"
            dataKey="requests"
            name="请求数"
            stroke="var(--chart-1)"
            fill="url(#gReq)"
            strokeWidth={2}
          />
          <Area
            type="monotone"
            dataKey="failed"
            name="失败"
            stroke="var(--destructive)"
            fill="none"
            strokeWidth={1.5}
            strokeDasharray="4 3"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
