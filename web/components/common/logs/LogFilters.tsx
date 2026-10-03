'use client';

import * as React from 'react';
import {Search, X} from 'lucide-react';

import {Input} from '@/components/ui/input';
import {cn} from '@/lib/utils';
import type {LogQuery} from '@/hooks/use-logs';

/** 级别筛选（值空串 = 全部）。 */
const LEVEL_OPTIONS: Array<{value: string; label: string}> = [
  {value: '', label: '全部级别'},
  {value: 'INFO', label: 'INFO'},
  {value: 'WARN', label: 'WARN'},
  {value: 'ERROR', label: 'ERROR'},
];

/** 固定模块筛选；日志里出现的新 tag 会追加在后面（见 extraTags）。 */
const TAG_OPTIONS: Array<{value: string; label: string}> = [
  {value: '', label: '全部模块'},
  {value: 'chat', label: '对话'},
  {value: 'tasks', label: '任务福利'},
  {value: 'scheduler', label: '调度器'},
  {value: 'accounts', label: '账号'},
  {value: 'system', label: '系统'},
];

function Pill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors',
        active
          ? 'bg-primary font-semibold text-primary-foreground shadow-xs'
          : 'text-muted-foreground hover:bg-background/70 hover:text-foreground',
      )}
    >
      {children}
    </button>
  );
}

export function LogFilters({
  query,
  onChange,
  extraTags,
  matched,
  total,
}: {
  query: LogQuery;
  onChange: (patch: Partial<LogQuery>) => void;
  /** 日志里出现但不在固定列表中的 tag（catalog / auth / settings 等）。 */
  extraTags: string[];
  matched: number;
  total: number;
}) {
  const tags = React.useMemo(() => {
    const known = new Set(TAG_OPTIONS.map((t) => t.value));
    return [
      ...TAG_OPTIONS,
      ...extraTags.filter((t) => !known.has(t)).map((t) => ({value: t, label: t})),
    ];
  }, [extraTags]);

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-[20px] border border-border/60 bg-card px-3 py-2.5 shadow-[0_1px_2px_rgba(15,23,42,0.04)] dark:border-border/70">
      <div className="inline-flex flex-wrap items-center gap-0.5 rounded-full bg-muted/60 p-0.5">
        {LEVEL_OPTIONS.map((opt) => (
          <Pill
            key={opt.value || 'all'}
            active={query.level === opt.value}
            onClick={() => onChange({level: opt.value})}
          >
            {opt.label}
          </Pill>
        ))}
      </div>

      <div className="inline-flex flex-wrap items-center gap-0.5 rounded-full bg-muted/60 p-0.5">
        {tags.map((opt) => (
          <Pill
            key={opt.value || 'all'}
            active={query.tag === opt.value}
            onClick={() => onChange({tag: opt.value})}
          >
            {opt.label}
          </Pill>
        ))}
      </div>

      <div className="relative min-w-[180px] flex-1">
        <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query.search}
          onChange={(e) => onChange({search: e.target.value})}
          placeholder="过滤关键字（如 qwen、token、401、error）..."
          className="h-8 rounded-full pr-7 pl-8 text-xs"
          aria-label="日志关键字过滤"
        />
        {query.search && (
          <button
            type="button"
            aria-label="清除关键字"
            onClick={() => onChange({search: ''})}
            className="absolute top-1/2 right-2 grid size-4 -translate-y-1/2 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <X className="size-3" />
          </button>
        )}
      </div>

      <span className="text-[11px] text-muted-foreground">
        显示 {matched} / {total} 条
      </span>
    </div>
  );
}
