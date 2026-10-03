'use client';

import * as React from 'react';
import {
  Activity,
  Loader2,
  Power,
  RefreshCw,
  Trash2,
  Wallet,
  Zap,
} from 'lucide-react';

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {api} from '@/lib/api';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';

/**
 * 账号表格（「网关与运维」页）。
 *
 * 行为基准是旧看板 dashboard.html：
 *   · 行内动作全部是单账号入口；批量动作（卡片头部）都必须同时有行内入口
 *     ——历史事故：批量签到后列表被切到另一个区域，用户找不到原来那行；
 *   · 动作完成后只调用 onChanged() 原地刷新，不整页重载、不改变当前区域。
 */

/** 额度快照（后端 Account.fetch_credits 写入，随 public() 下发）。 */
export interface AccountCredits {
  remain?: number;
  used?: number;
  size?: number;
  exceeded?: boolean;
  usage_pct?: number | null;
  updated_at?: number;
  updated_iso?: string;
}

/** 后端 Account.public()（qoder2api/accounts.py）的返回形状。 */
export interface AccountRow {
  uid: string;
  nickname: string;
  domain: string;
  realm: string;
  platform: string;
  enabled: boolean;
  source: string;
  tokenFamily?: string;
  expiresAt: number | null;
  expiresIn: string | null;
  hasRefreshToken: boolean;
  hasPAT: boolean;
  lastError: string;
  inFlight: number;
  consecutiveFailures: number;
  inCooldown: boolean;
  cooldownFor: number | null;
  addedAt?: number | null;
  file?: string | null;
  credits?: AccountCredits | null;
  plan?: string;
  lastCheckin?: string | null;
  canCheckin?: boolean | null;
  checkinCapability?: string;
  checkinReason?: string;
  userType?: string;
  machineId?: string;
  sessionId?: string;
}

interface ActionResult {
  uid?: string;
  ok?: boolean;
  error?: string;
  msg?: string;
}

type Tone = 'ok' | 'warning' | 'danger' | 'muted';

const TONE_BADGE: Record<Tone, string> = {
  ok: 'border-emerald-600/30 bg-emerald-600/10 text-emerald-700 dark:text-emerald-400',
  warning: 'border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  danger: 'border-red-500/35 bg-red-500/10 text-red-700 dark:text-red-400',
  muted: 'border-border/60 text-muted-foreground',
};

const REALM_BADGE: Record<string, string> = {
  intl: 'border-sky-600/30 bg-sky-600/10 text-sky-700 dark:text-sky-400',
  cn: 'border-red-600/30 bg-red-600/10 text-red-700 dark:text-red-400',
};

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function fmt(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString('en-US');
}

function uid8(uid: string): string {
  return (uid || '').slice(0, 8);
}

/** 有效期：把后端的 "21 days / 3.5 hours / 45 min / expired" 转成中文并判断是否临近过期。 */
function expiry(row: AccountRow): {text: string; tone: Tone} {
  const s = row.expiresIn;
  if (!s) return {text: '—', tone: 'muted'};
  if (s === 'expired') return {text: '已过期', tone: 'danger'};
  const m = /^([\d.]+)\s*(days|hours|min)$/.exec(s);
  if (!m) return {text: s, tone: 'muted'};
  const value = Number(m[1]);
  const unit = m[2];
  const seconds =
    unit === 'days' ? value * 86400 : unit === 'hours' ? value * 3600 : value * 60;
  const unitCn = unit === 'days' ? '天' : unit === 'hours' ? '小时' : '分钟';
  return {text: `${m[1]} ${unitCn}`, tone: seconds <= 48 * 3600 ? 'warning' : 'ok'};
}

function stateOf(row: AccountRow): {text: string; className: string} {
  if (!row.enabled) return {text: '已停用', className: TONE_BADGE.muted};
  if (row.inCooldown) {
    return {
      text: `冷却 ${Math.round(row.cooldownFor ?? 0)}s`,
      className: TONE_BADGE.warning,
    };
  }
  if (row.expiresIn === 'expired') return {text: '已过期', className: TONE_BADGE.danger};
  return {text: '可用', className: TONE_BADGE.ok};
}

export function AccountTable({
  rows,
  busy,
  onCheckin,
  onChanged,
}: {
  rows: AccountRow[];
  /** 正在执行签到动作的 uid，或 'all'，或 ''。 */
  busy: string;
  onCheckin: (uid?: string) => Promise<void>;
  onChanged: () => Promise<void> | void;
}) {
  const [pending, setPending] = React.useState<Record<string, boolean>>({});
  const [batchBusy, setBatchBusy] = React.useState<'' | 'credits' | 'enable' | 'disable'>('');
  const [deleteTarget, setDeleteTarget] = React.useState<AccountRow | null>(null);
  const [deleting, setDeleting] = React.useState(false);

  const isPending = (uid: string, action: string) => !!pending[`${action}:${uid}`];

  const runRow = (key: string, fn: () => Promise<void>) => {
    if (pending[key]) return;
    setPending((p) => ({...p, [key]: true}));
    void (async () => {
      try {
        await fn();
      } catch (e) {
        notify.err('操作失败', errText(e));
      } finally {
        setPending((p) => {
          const next = {...p};
          delete next[key];
          return next;
        });
      }
    })();
  };

  /** 单账号签到：页面级 onCheckin 负责请求 / 提示 / 原地刷新，这里只加防重锁。 */
  const checkinOne = (row: AccountRow) =>
    runRow(`checkin:${row.uid}`, async () => {
      await onCheckin(row.uid);
    });

  const testOne = (row: AccountRow) =>
    runRow(`test:${row.uid}`, async () => {
      const r = (await api.accounts.test(row.uid)) as {
        ok?: boolean;
        model?: string;
        elapsed_ms?: number;
        reply?: string;
        error?: string;
        status?: number;
      };
      if (r?.ok) {
        notify.ok(
          '连通正常',
          `${r.elapsed_ms ?? '?'}ms · ${r.model || 'auto'}${r.reply ? ` · ${r.reply}` : ''}`,
        );
      } else {
        notify.err('连通测试失败', String(r?.error || r?.status || '未知错误'));
      }
      await onChanged();
    });

  const refreshCreditsOne = (row: AccountRow) =>
    runRow(`credits:${row.uid}`, async () => {
      const r = (await api.accounts.credits(row.uid)) as {results?: ActionResult[]};
      const result = (r?.results || [])[0];
      if (!result) {
        notify.warn('未刷新额度', '没找到该账号，请先刷新列表');
      } else if (result.ok === false) {
        notify.warn('额度刷新失败', String(result.error || '未知原因'));
      } else {
        notify.ok('额度快照已刷新', row.nickname || uid8(row.uid));
      }
      await onChanged();
    });

  const refreshTokenOne = (row: AccountRow) =>
    runRow(`token:${row.uid}`, async () => {
      const r = (await api.accounts.refresh(row.uid)) as {results?: ActionResult[]};
      const result = (r?.results || [])[0];
      if (!result) {
        notify.warn('未刷新凭证', '没找到该账号，请先刷新列表');
      } else if (result.ok) {
        notify.ok('凭证刷新成功', row.nickname || uid8(row.uid));
      } else {
        notify.err('凭证刷新失败', String(result.error || '未知错误'));
      }
      await onChanged();
    });

  const toggleOne = (row: AccountRow) =>
    runRow(`toggle:${row.uid}`, async () => {
      const next = !row.enabled;
      await api.accounts.set(row.uid, {enabled: next});
      notify.ok(next ? '账号已启用' : '账号已停用', row.nickname || uid8(row.uid));
      await onChanged();
    });

  const confirmDelete = async () => {
    const row = deleteTarget;
    if (!row || deleting) return;
    setDeleting(true);
    try {
      await api.accounts.remove(row.uid);
      notify.ok('账号已删除', `${row.nickname || '?'}（${uid8(row.uid)}）`);
      setDeleteTarget(null);
      await onChanged();
    } catch (e) {
      notify.err('删除失败', errText(e));
    } finally {
      setDeleting(false);
    }
  };

  /** 批量刷新额度 = 全部账号（旧看板同款：只刷当前区域会让用户以为“点了没用”）。 */
  const refreshAllCredits = () => {
    if (batchBusy) return;
    setBatchBusy('credits');
    void (async () => {
      try {
        const r = (await api.accounts.credits()) as {results?: ActionResult[]};
        const results = r?.results || [];
        const failed = results.filter((x) => !x.ok);
        if (!results.length) {
          notify.warn('没有可刷新额度的账号');
        } else if (!failed.length) {
          notify.ok(`已刷新全部 ${results.length} 个账号的额度`);
        } else {
          notify.warn(
            `额度刷新 ${results.length - failed.length}/${results.length} 个账号`,
            String(failed[0]?.error || '部分账号失败').slice(0, 100),
          );
        }
        await onChanged();
      } catch (e) {
        notify.err('刷新额度失败', errText(e));
      } finally {
        setBatchBusy('');
      }
    })();
  };

  /** 批量启用 / 停用：后端 set-all 对所有区域的账号生效，文案里说明清楚。 */
  const setAllEnabled = (enabled: boolean) => {
    if (batchBusy) return;
    setBatchBusy(enabled ? 'enable' : 'disable');
    void (async () => {
      try {
        await api.accounts.setAll({enabled});
        notify.ok(enabled ? '已启用所有区域的账号' : '已停用所有区域的账号');
        await onChanged();
      } catch (e) {
        notify.err('批量操作失败', errText(e));
      } finally {
        setBatchBusy('');
      }
    })();
  };

  const enabledCount = rows.filter((r) => r.enabled).length;
  const coolingCount = rows.filter((r) => r.enabled && r.inCooldown).length;
  const checkinCount = rows.filter((r) => r.canCheckin !== false).length;

  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-2 pt-3">
        <div className="text-[11px] text-muted-foreground">
          共 {rows.length} 个 · 启用 {enabledCount} · 冷却 {coolingCount} · 待签到 {checkinCount}
          <span className="ml-2 hidden opacity-70 sm:inline">
            所有操作原地生效，不会切换区域
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-0.5">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 rounded-full text-xs"
            disabled={batchBusy !== ''}
            title="刷新所有区域账号的额度快照"
            onClick={refreshAllCredits}
          >
            {batchBusy === 'credits' ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Wallet className="size-3.5" />
            )}
            全部刷新额度
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 rounded-full text-xs"
            disabled={batchBusy !== ''}
            title="对所有区域的账号生效"
            onClick={() => setAllEnabled(true)}
          >
            {batchBusy === 'enable' ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Power className="size-3.5" />
            )}
            全部启用
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 rounded-full text-xs"
            disabled={batchBusy !== ''}
            title="对所有区域的账号生效"
            onClick={() => setAllEnabled(false)}
          >
            {batchBusy === 'disable' ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Power className="size-3.5" />
            )}
            全部停用
          </Button>
        </div>
      </div>

      <Table>
        <TableHeader>
          <TableRow className="border-b border-border/60 hover:bg-transparent">
            <TableHead className="pl-4 text-[11px] font-normal text-muted-foreground">
              账号
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              区域
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              套餐 / 额度
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              有效期
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              状态
            </TableHead>
            <TableHead className="pr-4 text-right text-[11px] font-normal text-muted-foreground">
              操作
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.length === 0 ? (
            <TableRow className="border-0 hover:bg-transparent">
              <TableCell colSpan={6} className="py-10 text-center text-xs text-muted-foreground">
                暂无账号
              </TableCell>
            </TableRow>
          ) : (
            rows.map((row) => {
              const exp = expiry(row);
              const state = stateOf(row);
              const checkinBusy = busy === 'all' || busy === row.uid;
              const checkinLocked = checkinBusy || isPending(row.uid, 'checkin');
              const noCheckinCap = row.checkinCapability === 'not_found';
              const cred = row.credits || null;
              const exhausted =
                cred?.exceeded === true ||
                (!!cred && (cred.size ?? 0) > 0 && (cred.remain ?? 0) <= 0);
              return (
                <TableRow
                  key={row.uid}
                  className={cn('border-b border-border/40', !row.enabled && 'opacity-70')}
                >
                  <TableCell className="pl-4 align-top">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-sm font-medium">
                        {row.nickname || uid8(row.uid)}
                      </span>
                      {row.enabled && row.canCheckin !== false && !noCheckinCap && (
                        <Badge
                          variant="outline"
                          className={cn('rounded-full px-1.5 py-0 text-[10px]', TONE_BADGE.warning)}
                        >
                          待签到
                        </Badge>
                      )}
                    </div>
                    <div
                      className="font-mono text-[11px] text-muted-foreground"
                      title={row.machineId ? `设备码 ${row.machineId}` : undefined}
                    >
                      {uid8(row.uid)} · {row.source || '未知来源'}
                      {row.tokenFamily ? ` · ${row.tokenFamily}` : ''}
                    </div>
                    {row.machineId && (
                      <div className="font-mono text-[10px] text-muted-foreground/70">
                        设备码 {row.machineId.slice(0, 14)}…
                      </div>
                    )}
                  </TableCell>

                  <TableCell className="align-top">
                    <Badge
                      variant="outline"
                      className={cn('rounded-full text-[11px]', REALM_BADGE[row.realm])}
                    >
                      {row.realm === 'cn' ? '国内版' : '国际版'}
                    </Badge>
                  </TableCell>

                  <TableCell className="align-top">
                    {row.plan ? (
                      <Badge variant="secondary" className="rounded-full text-[10px]">
                        {row.plan}
                      </Badge>
                    ) : null}
                    {cred ? (
                      <div
                        className="mt-1 text-xs tabular-nums"
                        title={cred.updated_iso ? `更新于 ${cred.updated_iso}` : undefined}
                      >
                        <b
                          className={cn(
                            exhausted
                              ? 'text-red-600 dark:text-red-400'
                              : 'text-emerald-600 dark:text-emerald-400',
                          )}
                        >
                          {fmt(cred.remain)}
                        </b>
                        <span className="text-muted-foreground"> / {fmt(cred.size)}</span>
                        {exhausted && (
                          <span className="ml-1 text-[10px] text-red-600 dark:text-red-400">
                            已耗尽
                          </span>
                        )}
                      </div>
                    ) : (
                      <div className="mt-1 text-xs text-muted-foreground">未查询</div>
                    )}
                  </TableCell>

                  <TableCell className="align-top">
                    <span
                      className={cn(
                        'text-xs tabular-nums',
                        exp.tone === 'ok' && 'text-foreground',
                        exp.tone === 'warning' && 'text-amber-600 dark:text-amber-400',
                        exp.tone === 'danger' && 'text-red-600 dark:text-red-400',
                        exp.tone === 'muted' && 'text-muted-foreground',
                      )}
                    >
                      {exp.text}
                    </span>
                  </TableCell>

                  <TableCell className="align-top">
                    <Badge
                      variant="outline"
                      className={cn('rounded-full text-[11px]', state.className)}
                      title={
                        row.consecutiveFailures > 1
                          ? `连续失败 ${row.consecutiveFailures} 次`
                          : undefined
                      }
                    >
                      {state.text}
                    </Badge>
                    {row.lastError && (
                      <div
                        className="mt-1 max-w-[220px] truncate text-[10px] text-amber-600 dark:text-amber-400"
                        title={row.lastError}
                      >
                        {row.lastError}
                      </div>
                    )}
                    {row.lastCheckin && (
                      <div className="mt-0.5 text-[10px] text-muted-foreground">
                        最近签到 {row.lastCheckin}
                      </div>
                    )}
                    {noCheckinCap && (
                      <div
                        className="mt-0.5 max-w-[220px] truncate text-[10px] text-muted-foreground"
                        title={row.checkinReason || '本区域没有签到接口'}
                      >
                        本区无签到入口
                      </div>
                    )}
                  </TableCell>

                  <TableCell className="pr-4 align-top text-right">
                    <div className="flex flex-wrap items-center justify-end gap-0.5">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 rounded-full px-2 text-xs"
                        disabled={checkinLocked || noCheckinCap}
                        title={
                          noCheckinCap
                            ? row.checkinReason || '本区域没有签到接口'
                            : '只签到这一个账号，不切换区域'
                        }
                        onClick={() => checkinOne(row)}
                      >
                        {checkinLocked ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <Zap className="size-3.5" />
                        )}
                        签到
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 rounded-full px-2 text-xs"
                        disabled={isPending(row.uid, 'test')}
                        title="向该账号发一条测试请求"
                        onClick={() => testOne(row)}
                      >
                        {isPending(row.uid, 'test') ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <Activity className="size-3.5" />
                        )}
                        测试
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 rounded-full px-2 text-xs"
                        disabled={isPending(row.uid, 'credits')}
                        title="刷新该账号的积分与套餐快照"
                        onClick={() => refreshCreditsOne(row)}
                      >
                        {isPending(row.uid, 'credits') ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <Wallet className="size-3.5" />
                        )}
                        刷新额度
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 rounded-full px-2 text-xs"
                        disabled={isPending(row.uid, 'token')}
                        title="刷新该账号的登录凭证（access token）"
                        onClick={() => refreshTokenOne(row)}
                      >
                        {isPending(row.uid, 'token') ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <RefreshCw className="size-3.5" />
                        )}
                        刷新凭证
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 rounded-full px-2 text-xs"
                        disabled={isPending(row.uid, 'toggle')}
                        onClick={() => toggleOne(row)}
                      >
                        {isPending(row.uid, 'toggle') ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <Power className="size-3.5" />
                        )}
                        {row.enabled ? '停用' : '启用'}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 rounded-full px-2 text-xs text-red-600 hover:bg-red-500/10 hover:text-red-600 dark:text-red-400 dark:hover:text-red-400"
                        title="删除该账号的本地凭证文件"
                        onClick={() => setDeleteTarget(row)}
                      >
                        <Trash2 className="size-3.5" />
                        删除
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>

      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open && !deleting) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除账号？</AlertDialogTitle>
            <AlertDialogDescription>
              将删除「{deleteTarget?.nickname || uid8(deleteTarget?.uid || '')}」（
              {uid8(deleteTarget?.uid || '')}）及其本地凭证文件，此操作不可撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>取消</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleting}
              className="bg-destructive text-white hover:bg-destructive/90"
              onClick={async (e) => {
                e.preventDefault();
                await confirmDelete();
              }}
            >
              {deleting ? '删除中…' : '确认删除'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
