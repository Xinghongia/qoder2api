'use client';

import * as React from 'react';
import {Copy, Download, RefreshCw} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {Button} from '@/components/ui/button';
import {ClearLogsDialog} from '@/components/common/logs/ClearLogsDialog';
import {LogFilters} from '@/components/common/logs/LogFilters';
import {LogTerminal} from '@/components/common/logs/LogTerminal';
import {api, getApiKey} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {withBasePath} from '@/lib/base-path';
import {notify} from '@/lib/toast';
import {
  copyTextToClipboard,
  filterLogs,
  formatLogsText,
  useLogs,
  type LogQuery,
} from '@/hooks/use-logs';

export default function LogsPage() {
  const {ready, needsLogin, sessionEpoch} = useAuth();
  const authed = ready && !needsLogin;
  const {entries, live, setLive, loading, status, refresh, clearLocal, stats} = useLogs(
    authed,
    sessionEpoch,
  );

  const [filters, setFilters] = React.useState<LogQuery>({level: '', tag: '', search: ''});
  const [autoScroll, setAutoScroll] = React.useState(true);

  // 输入框即时回显，筛选走 deferred 值，避免长列表过滤卡住打字。
  const deferredSearch = React.useDeferredValue(filters.search);
  const effectiveQuery = React.useMemo<LogQuery>(
    () => ({level: filters.level, tag: filters.tag, search: deferredSearch}),
    [filters.level, filters.tag, deferredSearch],
  );
  const filtered = React.useMemo(
    () => filterLogs(entries, effectiveQuery),
    [entries, effectiveQuery],
  );

  // 日志里出现过的新模块（catalog / auth / settings 等）也补进筛选项。
  const extraTags = React.useMemo(() => {
    const seen = new Set<string>();
    entries.forEach((item) => {
      if (item.tag) seen.add(item.tag);
    });
    return Array.from(seen);
  }, [entries]);

  const onFilterChange = React.useCallback((patch: Partial<LogQuery>) => {
    setFilters((prev) => ({...prev, ...patch}));
  }, []);

  // 导出走浏览器顶层导航：后端 /logs/export 直接回 Content-Disposition 附件。
  // 顶层导航无法带 Authorization 头，用 ?key= 兜底（后端 _supplied_key 支持）。
  const exportHref = React.useMemo(() => {
    const base = withBasePath('/logs/export');
    const key = getApiKey();
    return key ? `${base}?key=${encodeURIComponent(key)}` : base;
  }, []);

  const onCopy = React.useCallback(async () => {
    if (!filtered.length) {
      notify.warn('当前没有可复制的日志');
      return;
    }
    try {
      await copyTextToClipboard(formatLogsText(filtered));
      notify.ok(`已复制 ${filtered.length} 行日志`);
    } catch (e) {
      notify.err('复制失败', e instanceof Error ? e.message : String(e));
    }
  }, [filtered]);

  const onClear = React.useCallback(async () => {
    try {
      await api.logsClear();
      clearLocal();
      notify.ok('网关日志已清空');
    } catch (e) {
      notify.err('清空失败', e instanceof Error ? e.message : String(e));
      throw e;
    }
  }, [clearLocal]);

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="网关运行日志"
        description="实时捕获反向代理请求、流式转发、调度巡检、任务打卡与异常告警（内存保留最新 2000 条）。"
        actions={
          <>
            <span className="mr-1 flex items-center gap-2 text-xs text-muted-foreground">
              共 {stats.total} 条
              {stats.errors > 0 && (
                <span className="font-medium text-red-500">{stats.errors} 错误</span>
              )}
              {stats.warns > 0 && (
                <span className="font-medium text-amber-500">{stats.warns} 警告</span>
              )}
            </span>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void refresh()}
              title="立即全量拉取最新日志"
            >
              <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
              刷新
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setLive((v) => !v)}
              title={live ? '暂停每 2 秒的增量拉取' : '恢复实时监听'}
              className={
                live
                  ? 'border-emerald-500/40 text-emerald-600 dark:text-emerald-400'
                  : 'text-muted-foreground'
              }
            >
              <span
                className={
                  live
                    ? 'size-1.5 animate-pulse rounded-full bg-emerald-500'
                    : 'size-1.5 rounded-full bg-muted-foreground'
                }
              />
              {live ? '实时监听中' : '已暂停监听'}
            </Button>
            <Button
              size="sm"
              variant={autoScroll ? 'secondary' : 'outline'}
              onClick={() => setAutoScroll((v) => !v)}
              title="新日志到达时自动滚动到底部"
            >
              自动滚屏: {autoScroll ? '开' : '关'}
            </Button>
            <Button size="sm" variant="outline" onClick={() => void onCopy()} title="复制当前视图显示的日志">
              <Copy className="size-4" />
              复制
            </Button>
            <Button size="sm" variant="outline" asChild title="下载为 .log 文件">
              <a href={exportHref}>
                <Download className="size-4" />
                导出
              </a>
            </Button>
            <ClearLogsDialog count={stats.total} onConfirm={onClear} />
          </>
        }
      />

      <LogFilters
        query={filters}
        onChange={onFilterChange}
        extraTags={extraTags}
        matched={filtered.length}
        total={entries.length}
      />

      <LogTerminal
        entries={filtered}
        loading={loading}
        status={status}
        live={live}
        autoScroll={autoScroll}
      />
    </div>
  );
}
