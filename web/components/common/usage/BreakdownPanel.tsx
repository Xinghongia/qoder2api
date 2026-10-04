'use client';

import * as React from 'react';

import {EmptyState} from '@/components/common/layout/EmptyState';
import {fmt} from '@/components/common/stats/format';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import type {LucideIcon} from 'lucide-react';

import {seriesColor} from './types';

/** 一行：名称 + 请求数 + Token + 积分（后端 by_model / by_key 的同构字段）。 */
export interface BreakdownRow {
  name: string;
  requests: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  credit: number;
}

/**
 * 「按模型 / 按密钥」明细表 —— 设计基准是 workbuddy 管理面板统计页的
 * BreakdownPanel：名称列内画彩色圆点 + 进度条（宽度 = 该项 Token / 最大值），
 * 不上百分比数字；右侧依次是请求数、Token、积分。
 */
export function BreakdownPanel({
  title,
  icon: Icon,
  rows,
  loading,
  emptyText,
}: {
  title: string;
  icon: LucideIcon;
  rows: BreakdownRow[];
  loading: boolean;
  emptyText: string;
}) {
  const max = rows.reduce((n, r) => Math.max(n, r.total_tokens), 0) || 1;
  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <div className="flex items-center gap-2 px-4 pb-2 pt-3">
        <Icon className="size-3.5 text-muted-foreground" />
        <h2 className="text-sm font-medium">{title}</h2>
        <span className="text-[11px] text-muted-foreground">（{rows.length} 项）</span>
      </div>
      {!rows.length && !loading ? (
        // 空态必须在一个**有确定高度**的容器里居中：EmptyState 默认的 h-full
        // 在高度 auto 的 section 里解析不到，内容会贴着上/下边缘（实测偏下）。
        <div className="grid min-h-[200px] place-items-center px-4 pb-4">
          <EmptyState
            icon={Icon}
            title={emptyText}
            className="flex flex-col items-center justify-center text-center"
          />
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] font-normal text-muted-foreground">
                名称
              </TableHead>
              <TableHead className="text-right text-[11px] font-normal text-muted-foreground">
                请求数
              </TableHead>
              <TableHead className="text-right text-[11px] font-normal text-muted-foreground">
                Token
              </TableHead>
              <TableHead className="pr-4 text-right text-[11px] font-normal text-muted-foreground">
                积分
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r, i) => (
              <TableRow key={`${r.name}-${i}`} className="border-b border-border/40">
                <TableCell className="pl-4">
                  <div className="flex items-center gap-2">
                    <span
                      aria-hidden
                      className="size-2 shrink-0 rounded-full"
                      style={{background: seriesColor(i)}}
                    />
                    <span className="max-w-[180px] truncate text-xs" title={r.name}>
                      {r.name}
                    </span>
                  </div>
                  {/* 进度条：按 Token 占最大项的比例（workbuddy 同款表达） */}
                  <div className="mt-1 h-1 w-full max-w-[220px] overflow-hidden rounded-full bg-background/70">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${Math.max(2, Math.round((r.total_tokens / max) * 100))}%`,
                        background: seriesColor(i),
                        opacity: 0.7,
                      }}
                    />
                  </div>
                </TableCell>
                <TableCell className="text-right text-xs tabular-nums">
                  {fmt(r.requests)}
                </TableCell>
                <TableCell className="text-right text-xs tabular-nums">
                  {fmt(r.total_tokens)}
                  <div className="text-[10px] text-muted-foreground">
                    {fmt(r.prompt_tokens)} / {fmt(r.completion_tokens)}
                  </div>
                </TableCell>
                <TableCell className="pr-4 text-right text-xs tabular-nums">
                  {(r.credit ?? 0).toFixed(2)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}
