'use client';

import * as React from 'react';
import {ChevronDown, ChevronUp, ScrollText} from 'lucide-react';

import {cn} from '@/lib/utils';

/**
 * 任务日志框（「签到与福利中心」）。
 *
 * 行为对齐旧看板 dashboard.html 的 #taskLogBox：
 *   · 领取动作完成后逐行展示日志，并自动滚动到最新一行；
 *   · ✓ / ! / ⚠ / ====== / ------ 前缀分别着色，失败行一眼可见。
 */

function lineClass(line: string): string {
  const s = line.trimStart();
  if (s.startsWith('======') || s.startsWith('------')) {
    return 'font-semibold text-foreground';
  }
  if (s.startsWith('✓')) return 'text-emerald-600 dark:text-emerald-400';
  if (s.startsWith('!')) return 'text-red-600 dark:text-red-400';
  if (s.startsWith('⚠')) return 'text-amber-600 dark:text-amber-400';
  return 'text-muted-foreground';
}

export function TaskLogBox({
  lines,
  title = '任务日志',
}: {
  /** 逐行日志；为空时整个日志框不渲染。 */
  lines: string[];
  title?: string;
}) {
  const [open, setOpen] = React.useState(true);
  const boxRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    const el = boxRef.current;
    if (el && open) el.scrollTop = el.scrollHeight;
  }, [lines, open]);

  if (!lines.length) return null;

  return (
    <section className="rounded-[20px] bg-muted px-3.5 py-3 sm:px-4">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
      >
        {open ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
        <ScrollText className="size-3.5" />
        {title}（{lines.length} 行{open ? '' : '，点击展开'}）
      </button>

      {open && (
        <div
          ref={boxRef}
          className="scroll-slim mt-2 max-h-56 space-y-0.5 overflow-y-auto rounded-[12px] bg-background/60 px-3 py-2 font-mono text-[11px] leading-5"
        >
          {lines.map((line, i) => (
            <div
              key={`${i}-${line.slice(0, 24)}`}
              className={cn('whitespace-pre-wrap break-all', lineClass(line))}
              title={line}
            >
              {line || ' '}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
