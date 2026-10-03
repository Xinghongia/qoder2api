'use client';

import * as React from 'react';
import {Loader2, Monitor, RefreshCw, ShieldAlert} from 'lucide-react';

import {ApiError, http} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useAuthedLoad} from '@/lib/use-authed-load';
import {cn} from '@/lib/utils';

/**
 * 本机虚拟化检测卡（「签到与福利中心」四张状态卡之一）。
 *
 * 数据来自 GET /diag/vm（qoder2api/accounts.py 的 local_vm_status，中文输出）：
 *   · 以官方风控桥 runtime-info.exe 的 vmInfo 为准，桥不可用时后端退化成本机交叉校验
 *     （source === 'local'，卡片里明确标注）；
 *   · 「重新检测」带 force=1，绕过后端 300s 缓存重新探测；
 *   · 旧网关进程没有 /diag/vm 路由（404）时给出「需重启网关」的可操作提示。
 */

export interface VmStatus {
  is_vm?: boolean;
  /** 风险档位（高/中/低/无/未知）。 */
  level?: string;
  score?: number | null;
  brand?: string;
  brand_cn?: string;
  vm_type_code?: string | number | null;
  /** runtime-info / local / none */
  source?: string;
  evidence?: string[];
  summary?: string;
  realm?: string;
  bridge_available?: boolean;
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function VmStatusCard({realm}: {realm?: 'intl' | 'cn'}) {
  const [data, setData] = React.useState<VmStatus | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState('');

  const load = React.useCallback(
    async (force = false) => {
      setLoading(true);
      try {
        const q = new URLSearchParams();
        if (force) q.set('force', '1');
        if (realm) q.set('realm', realm);
        const qs = q.toString();
        const r = (await http.get(`/diag/vm${qs ? `?${qs}` : ''}`)) as VmStatus;
        setData(r || null);
        setError('');
      } catch (e) {
        const status = e instanceof ApiError ? e.status : 0;
        const msg =
          status === 404
            ? '该功能需要新版本网关进程：重启网关后点「重新检测」。'
            : errText(e);
        setError(msg);
        // 加载失败如实提示，但卡片与「重新检测」保持可用
        notify.err(
          '本机虚拟化检测失败',
          status === 404 ? '当前网关进程版本较旧，重启网关后再试' : errText(e),
        );
      } finally {
        setLoading(false);
      }
    },
    [realm],
  );

  useAuthedLoad(() => load(false), [load]);

  const isVm = data?.is_vm === true;
  const bridgeLocal = data?.source === 'local';
  const evidence = (data?.evidence || []).slice(0, 3);

  let value: React.ReactNode = '—';
  let valueClass = 'text-gray-900 dark:text-gray-100';
  if (loading && !data) {
    value = '检测中…';
    valueClass = 'text-muted-foreground';
  } else if (error && !data) {
    value = error.startsWith('该功能需要新版本') ? '需重启网关' : '检测失败';
    valueClass = 'text-amber-600 dark:text-amber-400';
  } else if (data) {
    if (isVm) {
      value = `虚拟机${data.brand_cn ? ` · ${data.brand_cn}` : ''}`;
      valueClass = 'text-amber-600 dark:text-amber-400';
    } else {
      value = '物理机';
      valueClass = 'text-emerald-600 dark:text-emerald-400';
    }
  }

  let hint = '本机虚拟化检测';
  if (data) {
    hint = isVm
      ? `风险档位 ${data.level || '-'} · 风控评分 ${data.score ?? '-'}/100`
      : '未检测到虚拟化运行环境';
  }

  return (
    <section
      className="min-h-[88px] rounded-[20px] bg-muted px-3.5 py-3 sm:min-h-[96px] sm:px-4"
      title={data?.summary || ''}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="truncate text-[11px] font-medium text-gray-500 dark:text-gray-400">
          {hint}
        </div>
        <div
          className={cn(
            'grid h-6 w-6 shrink-0 place-items-center rounded-full bg-white/70 dark:bg-white/[0.05]',
            isVm ? 'text-amber-600 dark:text-amber-400' : 'text-gray-500',
          )}
        >
          {isVm ? <ShieldAlert className="h-3.5 w-3.5" /> : <Monitor className="h-3.5 w-3.5" />}
        </div>
      </div>

      <div className={cn('mt-3 text-xl font-semibold tracking-[-0.03em] sm:text-2xl', valueClass)}>
        {value}
      </div>

      {(bridgeLocal || evidence.length > 0 || error) && (
        <div className="mt-2 space-y-0.5 text-[10.5px] leading-4 text-gray-500 dark:text-gray-400">
          {bridgeLocal && <div>官方风控桥不可用，以下为本机交叉校验</div>}
          {evidence.map((line, i) => (
            <div key={`${i}-${line.slice(0, 16)}`} className="break-all" title={line}>
              {line}
            </div>
          ))}
          {error && <div className="break-all text-amber-600 dark:text-amber-400">{error}</div>}
        </div>
      )}

      <div className="mt-2">
        <button
          type="button"
          disabled={loading}
          onClick={() => void load(true)}
          className="inline-flex h-6 items-center gap-1 rounded-full px-2 text-[11px] text-gray-500 transition-colors hover:bg-white/70 hover:text-foreground disabled:opacity-60 dark:text-gray-400 dark:hover:bg-white/[0.06]"
        >
          {loading ? <Loader2 className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
          重新检测
        </button>
      </div>
    </section>
  );
}
