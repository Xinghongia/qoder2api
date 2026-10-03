'use client';

import * as React from 'react';
import {ScrollText} from 'lucide-react';

import {EmptyState} from '@/components/common/layout/EmptyState';
import {type LogEntry} from '@/hooks/use-logs';

/**
 * 日志终端：复刻旧看板的深色控制台观感（mac 圆点 + 等宽字体 + 语义色），
 * 深浅色主题下都保持深底，容器圆角 20px。
 */

const LEVEL_STYLE: Record<string, React.CSSProperties> = {
  INFO: {background: 'rgba(37,99,235,.2)', color: '#60a5fa'},
  WARN: {background: 'rgba(217,119,6,.2)', color: '#fbbf24'},
  ERROR: {background: 'rgba(220,38,38,.25)', color: '#f87171'},
  DEBUG: {background: 'rgba(148,163,184,.2)', color: '#94a3b8'},
};
const LEVEL_FALLBACK: React.CSSProperties = {
  background: 'rgba(148,163,184,.2)',
  color: '#94a3b8',
};

const TAG_COLOR: Record<string, string> = {
  chat: '#c084fc',
  scheduler: '#4ade80',
  tasks: '#facc15',
  accounts: '#38bdf8',
  auth: '#fb7185',
  catalog: '#f9a8d4',
  settings: '#a5b4fc',
  system: '#94a3b8',
};

const LogRow = React.memo(function LogRow({entry}: {entry: LogEntry}) {
  const level = entry.level || 'INFO';
  const tag = entry.tag || 'system';
  return (
    <div className="flex items-start gap-2 rounded px-1.5 py-[3px] font-mono text-xs leading-[1.55] transition-colors hover:bg-white/5">
      <span className="shrink-0 tabular-nums text-slate-500 select-none">
        {entry.time || entry.ts || ''}
      </span>
      <span
        className="min-w-[46px] shrink-0 rounded px-1.5 py-px text-center text-[10px] font-bold select-none"
        style={LEVEL_STYLE[level] ?? LEVEL_FALLBACK}
      >
        {level}
      </span>
      <span
        className="shrink-0 rounded px-1 py-px text-[11px] font-semibold select-none"
        style={{background: 'rgba(148,163,184,.12)', color: TAG_COLOR[tag] ?? '#94a3b8'}}
      >
        [{tag}]
      </span>
      <span className="min-w-0 flex-1 break-all whitespace-pre-wrap text-slate-300">
        {entry.msg}
      </span>
    </div>
  );
});

export function LogTerminal({
  entries,
  loading,
  status,
  live,
  autoScroll,
}: {
  entries: LogEntry[];
  loading: boolean;
  status: string;
  live: boolean;
  autoScroll: boolean;
}) {
  const bodyRef = React.useRef<HTMLDivElement | null>(null);
  const [userScrolledUp, setUserScrolledUp] = React.useState(false);

  const handleScroll = React.useCallback(() => {
    const el = bodyRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (atBottom) setUserScrolledUp(false);
    else if (autoScroll) setUserScrolledUp(true);
  }, [autoScroll]);

  // 新日志追加后贴底；用户手动往上翻（或关掉自动滚屏）时不抢滚动。
  // 重新打开自动滚屏时 dependency 变化，会立即回到底部（与旧看板一致）。
  React.useEffect(() => {
    const el = bodyRef.current;
    if (!el || !autoScroll || userScrolledUp) return;
    el.scrollTop = el.scrollHeight;
  }, [entries, autoScroll, userScrolledUp]);

  return (
    // 终端恒定深色：即使页面处于亮色主题，也用 dark 作用域保证空态文字对比度。
    <div className="dark overflow-hidden rounded-[20px] border border-[#1e293b] bg-[#090d16] shadow-[0_4px_14px_rgba(0,0,0,0.15)]">
      <div className="flex items-center justify-between gap-3 border-b border-[#1e293b] bg-[#0f172a] px-3.5 py-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="size-2.5 shrink-0 rounded-full bg-[#ef4444]" />
          <span className="size-2.5 shrink-0 rounded-full bg-[#f59e0b]" />
          <span className="size-2.5 shrink-0 rounded-full bg-[#10b981]" />
          <span className="ml-2 truncate font-mono text-[11px] text-slate-400">
            wb-proxy-console.log
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          {!live && <span className="font-mono text-[11px] text-amber-400/90">已暂停监听</span>}
          <span className="font-mono text-[11px] text-slate-500">{status}</span>
        </div>
      </div>

      <div
        ref={bodyRef}
        onScroll={handleScroll}
        className="scroll-slim h-[520px] overflow-y-auto px-3 py-2.5 md:h-[620px]"
      >
        {loading && entries.length === 0 ? (
          <div className="px-1 py-1 font-mono text-xs text-slate-500">正在拉取网关日志...</div>
        ) : entries.length === 0 ? (
          <EmptyState
            icon={ScrollText}
            title="暂无匹配的日志记录"
            description="网关产生请求、巡检或告警后会自动出现在这里；也可以换个级别、模块或关键字试试。"
            className="flex h-full flex-col items-center justify-center p-8 text-center text-slate-300"
          />
        ) : (
          entries.map((entry) => <LogRow key={entry.id} entry={entry} />)
        )}
      </div>
    </div>
  );
}
