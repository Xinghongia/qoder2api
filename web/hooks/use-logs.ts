'use client';

import * as React from 'react';

import {api} from '@/lib/api';
import {notify} from '@/lib/toast';

/**
 * 网关运行日志：首屏全量拉取 + 之后按 since_id 增量轮询。
 *
 * 行为对齐旧看板 dashboard.html：
 *   · 打开页面先 GET /logs?since_id=0&limit=500 全量拉一次；
 *   · 之后每 2 秒带最后一条 id 增量拉取，新日志追加到列表尾部；
 *   · 本地最多保留 2000 条（与后端 LOG_BUFFER 容量一致）；
 *   · 「实时监听」关闭时只停轮询，手动刷新仍可用；
 *   · 组件卸载（切走页面）时定时器随 effect 清理，不再打接口。
 */

export interface LogEntry {
  id: number;
  /** 完整时间戳 `YYYY-MM-DD HH:MM:SS`（复制/导出用） */
  ts: string;
  /** 短时间 `HH:MM:SS`（终端展示用，后端附带） */
  time?: string;
  /** INFO / WARN / ERROR / DEBUG，后端按关键字自动判定 */
  level: string;
  /** chat / tasks / scheduler / accounts / catalog / auth / settings / system */
  tag: string;
  msg: string;
}

export interface LogQuery {
  level: string;
  tag: string;
  search: string;
}

export interface LogStats {
  total: number;
  errors: number;
  warns: number;
}

interface LogsResponse {
  total?: number;
  logs?: LogEntry[];
  max_id?: number;
}

export const LOG_INITIAL_LIMIT = 500;
export const LOG_POLL_LIMIT = 200;
export const LOG_BUFFER_MAX = 2000;
export const LOG_POLL_MS = 2000;

/** 客户端筛选：与旧看板一致（级别/模块精确匹配，关键字搜 msg + tag + 时间）。 */
export function filterLogs(entries: LogEntry[], query: LogQuery): LogEntry[] {
  const search = query.search.trim().toLowerCase();
  if (!query.level && !query.tag && !search) return entries;
  return entries.filter((item) => {
    if (query.level && item.level !== query.level) return false;
    if (query.tag && (item.tag || '').toLowerCase() !== query.tag.toLowerCase()) return false;
    if (search) {
      const haystack = `${item.msg || ''} ${item.tag || ''} ${item.ts || item.time || ''}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
}

/** 单行日志文本（复制 / 导出格式，与旧看板一致）。 */
export function formatLogLine(item: LogEntry): string {
  return `[${item.ts || item.time || ''}] [${item.level || 'INFO'}] [${item.tag || 'system'}] ${item.msg || ''}`;
}

export function formatLogsText(entries: LogEntry[]): string {
  return entries.map(formatLogLine).join('\n');
}

/**
 * 写剪贴板：navigator.clipboard 需要安全上下文（https / localhost），
 * 局域网 http 打开时不可用，回退到临时 textarea + execCommand（旧看板同款）。
 */
export async function copyTextToClipboard(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.appendChild(area);
  area.select();
  area.setSelectionRange(0, area.value.length);
  const ok = document.execCommand('copy');
  document.body.removeChild(area);
  if (!ok) throw new Error('浏览器拒绝了复制操作');
}

function mergeEntries(prev: LogEntry[], batch: LogEntry[]): LogEntry[] {
  if (!batch.length) return prev;
  const seen = new Set(prev.map((x) => x.id));
  const fresh = batch.filter((x) => !seen.has(x.id));
  if (!fresh.length) return prev;
  const next = [...prev, ...fresh];
  next.sort((a, b) => a.id - b.id);
  return next.length > LOG_BUFFER_MAX ? next.slice(-LOG_BUFFER_MAX) : next;
}

function nowLabel(): string {
  return new Date().toLocaleTimeString('zh-CN', {hour12: false});
}

export function useLogs(enabled: boolean, sessionEpoch = 0) {
  const [entries, setEntries] = React.useState<LogEntry[]>([]);
  const [live, setLive] = React.useState(true);
  const [loading, setLoading] = React.useState(true);
  // 终端右上角状态文案：「正在拉取... / 就绪 · 12:00:00 / 连接中断: ...」
  const [status, setStatus] = React.useState('正在拉取网关日志...');

  const lastIdRef = React.useRef(0);
  const inFlightRef = React.useRef(false);
  // 「清空」后让还在路上的旧响应作废，避免清完又被追加回来。
  const generationRef = React.useRef(0);

  const fetchLogs = React.useCallback(async (reset: boolean): Promise<void> => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    const generation = generationRef.current;
    if (reset) setStatus('正在拉取...');
    try {
      const since = reset ? 0 : lastIdRef.current;
      const limit = reset ? LOG_INITIAL_LIMIT : LOG_POLL_LIMIT;
      const data = (await api.logs(since, limit)) as LogsResponse | null;
      if (generation !== generationRef.current) return;
      const rawLogs = data?.logs;
      const batch: LogEntry[] = Array.isArray(rawLogs) ? rawLogs : [];
      const maxId = typeof data?.max_id === 'number' ? data.max_id : 0;
      const lastBatchId = batch.length ? batch[batch.length - 1].id : 0;

      if (reset) {
        // 全量结果本身就是准的：直接以返回的末尾 id 为准，
        // 顺带兼容「后端重启后计数器归零」的情况。
        setEntries(batch.slice(-LOG_BUFFER_MAX));
        lastIdRef.current = Math.max(maxId, lastBatchId);
      } else {
        setEntries((prev) => mergeEntries(prev, batch));
        lastIdRef.current = Math.max(lastIdRef.current, maxId, lastBatchId);
      }
      setStatus(`就绪 · ${nowLabel()}`);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setStatus(`连接中断: ${message}`);
      // 轮询期偶发失败只在终端状态栏提示，不弹通知刷屏；首屏失败才弹。
      if (reset) notify.err('日志加载失败', message);
    } finally {
      inFlightRef.current = false;
      setLoading(false);
    }
  }, []);

  /** 立即全量重拉（「刷新」按钮）。 */
  const refresh = React.useCallback(async (): Promise<void> => {
    await fetchLogs(true);
  }, [fetchLogs]);

  /** 清空后端后的本地复位：列表清空 + since_id 归零。 */
  const clearLocal = React.useCallback((): void => {
    generationRef.current += 1;
    lastIdRef.current = 0;
    setEntries([]);
    setStatus(`就绪 · ${nowLabel()}`);
  }, []);

  // 首屏（面板会话就绪后）全量拉一次；登录成功（sessionEpoch 变化）后重来一遍。
  React.useEffect(() => {
    if (!enabled) return;
    setLoading(true);
    void refresh();
  }, [enabled, sessionEpoch, refresh]);

  // 增量轮询；卸载 / 关闭实时监听 / 切页都会走到 cleanup 清掉定时器。
  React.useEffect(() => {
    if (!enabled || !live) return;
    const timer = window.setInterval(() => {
      void fetchLogs(false);
    }, LOG_POLL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, live, fetchLogs]);

  const stats = React.useMemo<LogStats>(() => {
    let errors = 0;
    let warns = 0;
    entries.forEach((item) => {
      if (item.level === 'ERROR') errors += 1;
      else if (item.level === 'WARN') warns += 1;
    });
    return {total: entries.length, errors, warns};
  }, [entries]);

  return {entries, live, setLive, loading, status, refresh, clearLocal, stats};
}
