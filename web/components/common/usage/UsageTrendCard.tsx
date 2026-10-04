'use client';

import * as React from 'react';
import {
  Area,
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {cn} from '@/lib/utils';

import {fmtCompact, seriesColor, type UsageMetric, type UsageStats} from './types';

/**
 * 「Token 消耗趋势」卡片（用量统计页）—— 设计基准是 workbuddy 管理面板的
 * 统计页趋势图：当日按小时 = 柱状（末柱高亮），较长范围 = 面积图，
 * 失败数叠一条红色虚线；网格只画横向、坐标轴无刻度线。
 *
 * 分组（总量 / 按模型 / 按密钥）对应后端 trend.series：多序列时用堆叠柱/
 * 堆叠面积，取前 N 条（其余后端已折叠成「其他」）。序列名里可能带点号
 * （如 `Qwen3.8-Flash`），recharts 的 dataKey 会当成嵌套路径解析，
 * 因此数据行统一用 `s0..sn` 下标键，展示名走 name={title}。
 */
export function UsageTrendCard({
  data,
  loading,
  group,
  metric,
  onGroupChange,
  onMetricChange,
}: {
  data: UsageStats | null;
  loading: boolean;
  group: 'total' | 'model' | 'key_id';
  metric: UsageMetric;
  onGroupChange: (g: 'total' | 'model' | 'key_id') => void;
  onMetricChange: (m: UsageMetric) => void;
}) {
  const trend = data?.trend;
  const labels = trend?.labels ?? [];
  const series = trend?.series ?? [];
  const hourly = trend?.granularity === 'hour';
  const multi = series.length > 1;

  const rows = React.useMemo(() => {
    return labels.map((label, i) => {
      const row: Record<string, number | string> = {label};
      series.forEach((s, si) => {
        row[`s${si}`] = metric === 'tokens' ? s.tokens[i] ?? 0 : s.requests[i] ?? 0;
      });
      row.failed = series.reduce((n, s) => n + (s.failed[i] ?? 0), 0);
      return row;
    });
  }, [labels, series, metric]);

  const total = series.reduce(
    (n, s) => n + (metric === 'tokens' ? s.tokens : s.requests).reduce((a, b) => a + b, 0),
    0,
  );
  // 当前小时（当日按小时视图）高亮：最后一个非零桶，没有数据时不亮
  const lastActive = React.useMemo(() => {
    if (!hourly || !rows.length) return -1;
    const sum = (r: Record<string, number | string>) =>
      series.reduce((n, _s, si) => n + Number(r[`s${si}`] ?? 0), 0);
    for (let i = rows.length - 1; i >= 0; i--) {
      if (sum(rows[i]) > 0) return i;
    }
    return -1;
  }, [hourly, rows, series]);

  const tickEvery = Math.max(1, Math.ceil(labels.length / 12));

  return (
    <section className="rounded-[20px] bg-muted p-4">
      <div className="flex flex-wrap items-center justify-between gap-2 pb-2">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="text-sm font-medium">Token 消耗趋势</h2>
          <span className="text-[11px] text-muted-foreground">
            {hourly
              ? '按小时（当日汇总）'
              : `按天（${data?.range.from ?? ''} ~ ${data?.range.to ?? ''}）`}
            {multi ? ` · 按${group === 'key_id' ? '密钥' : '模型'}堆叠` : ''}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={metric} onValueChange={(v) => onMetricChange(v as UsageMetric)}>
            {/* 「Token 消耗」四个汉字 + 下拉箭头，窄了会把文字截断成「Token 消」 */}
            <SelectTrigger size="sm" className="w-[136px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="tokens">Token 消耗</SelectItem>
              <SelectItem value="requests">请求量</SelectItem>
            </SelectContent>
          </Select>
          <Select value={group} onValueChange={(v) => onGroupChange(v as typeof group)}>
            <SelectTrigger size="sm" className="w-[128px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="total">总量</SelectItem>
              <SelectItem value="model">按模型</SelectItem>
              <SelectItem value="key_id">按密钥</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      {total === 0 && !loading ? (
        <div className="grid h-[260px] w-full place-items-center text-xs text-muted-foreground">
          该时间范围内还没有请求记录
        </div>
      ) : (
        <div className={cn('h-[260px] w-full', loading && 'opacity-60 transition-opacity')}>
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={rows} margin={{top: 4, right: 8, bottom: 0, left: -16}}>
              <defs>
                {series.map((s, i) => (
                  <linearGradient key={s.name} id={`ug${i}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={seriesColor(i)} stopOpacity={0.35} />
                    <stop offset="100%" stopColor={seriesColor(i)} stopOpacity={0.02} />
                  </linearGradient>
                ))}
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                fontSize={11}
                stroke="var(--muted-foreground)"
                interval={tickEvery - 1}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                fontSize={11}
                stroke="var(--muted-foreground)"
                tickFormatter={(v) => fmtCompact(Number(v))}
                allowDecimals={false}
              />
              <Tooltip
                contentStyle={{
                  background: 'var(--popover)',
                  border: '1px solid var(--border)',
                  borderRadius: 12,
                  fontSize: 12,
                }}
                labelFormatter={(label) =>
                  hourly ? `时间 ${label}` : `日期 ${label}`
                }
                formatter={(value, name) => [
                  Number(value).toLocaleString('en-US'),
                  String(name),
                ]}
              />
              {multi && (
                <Legend
                  wrapperStyle={{fontSize: 11, paddingTop: 4}}
                  iconType="circle"
                  iconSize={8}
                />
              )}
              {hourly
                ? series.map((s, i) => (
                    <Bar
                      key={s.name}
                      dataKey={`s${i}`}
                      name={s.title}
                      stackId={multi ? '1' : undefined}
                      fill={seriesColor(i)}
                      radius={multi ? undefined : [4, 4, 0, 0]}
                      maxBarSize={48}
                    >
                      {!multi &&
                        rows.map((_r, ri) => (
                          <Cell
                            key={ri}
                            fill={ri === lastActive ? 'var(--chart-2)' : seriesColor(0)}
                          />
                        ))}
                    </Bar>
                  ))
                : series.map((s, i) => (
                    <Area
                      key={s.name}
                      type="monotone"
                      dataKey={`s${i}`}
                      name={s.title}
                      stackId={multi ? '1' : undefined}
                      stroke={seriesColor(i)}
                      fill={`url(#ug${i})`}
                      strokeWidth={2}
                    />
                  ))}
              {metric === 'requests' && (
                <Line
                  type="monotone"
                  dataKey="failed"
                  name="失败"
                  stroke="var(--destructive)"
                  fill="none"
                  strokeWidth={1.5}
                  strokeDasharray="4 3"
                  dot={false}
                />
              )}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}
