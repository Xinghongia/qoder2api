'use client';

import * as React from 'react';
import {Copy, ExternalLink, Loader2, Ticket} from 'lucide-react';

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
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';

/**
 * 兑换码 / 券面板（「签到与福利中心」）。
 *
 * 行为对齐旧看板 dashboard.html 的 #codesPanel / #codesTable / copyText：
 *   · 按账号逐行展示已领取的兑换码，附「复制」按钮与「活动页/二维码」链接；
 *   · 复制必须走 navigator.clipboard，失败时回退 textarea + execCommand；
 *   · **只有确认写入成功才提示成功**（历史问题：复制没成功却报成功），
 *     两条路径都失败时如实提示用户手动选中复制。
 */

/** 后端 tasks.py 里 summary.codes 的条目形状（aggregate 与单账号共用）。 */
export interface RewardCode {
  /** 账号 uid（聚合视图下发；单账号视图可能缺省）。 */
  account?: string;
  nickname?: string;
  realm?: string;
  campaign?: string;
  campaign_id?: string;
  code?: string;
  /** 官方活动页（可在同一账号下查看二维码）。 */
  url?: string;
}

/**
 * 复制文本：clipboard API 优先，失败回退旧式 textarea。
 *
 * @returns 是否确认复制成功（两条路径都失败时为 false）。
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 无权限 / 非安全上下文：走下面的 textarea 回退 */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

const REALM_BADGE: Record<string, string> = {
  intl: 'border-sky-600/30 bg-sky-600/10 text-sky-700 dark:text-sky-400',
  cn: 'border-red-600/30 bg-red-600/10 text-red-700 dark:text-red-400',
};

const uid8 = (uid: string) => (uid || '').slice(0, 8);

export function RewardCodesPanel({codes}: {codes: RewardCode[]}) {
  const [copiedKey, setCopiedKey] = React.useState('');
  const [busyKey, setBusyKey] = React.useState('');
  const resetTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    return () => {
      if (resetTimer.current) clearTimeout(resetTimer.current);
    };
  }, []);

  if (!codes.length) return null;

  const onCopy = async (row: RewardCode, key: string) => {
    if (busyKey) return;
    setBusyKey(key);
    const ok = await copyText(row.code || '');
    setBusyKey('');
    if (ok) {
      setCopiedKey(key);
      notify.ok('兑换码已复制', row.nickname || uid8(row.account || ''));
      if (resetTimer.current) clearTimeout(resetTimer.current);
      resetTimer.current = setTimeout(() => {
        setCopiedKey((k) => (k === key ? '' : k));
      }, 1500);
    } else {
      // 如实提示：不要像旧版那样「复制没成功却报成功」
      notify.err('复制没成功', '浏览器拒绝了剪贴板写入，请手动选中兑换码复制');
    }
  };

  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <div className="flex flex-wrap items-center gap-2 px-4 pb-2 pt-3">
        <Ticket className="size-3.5 text-muted-foreground" />
        <span className="text-[13px] font-semibold">已领取的兑换码 / 券（按账号）</span>
        <span className="text-[11px] text-muted-foreground">共 {codes.length} 条</span>
      </div>

      <Table>
        <TableHeader>
          <TableRow className="border-b border-border/60 hover:bg-transparent">
            <TableHead className="pl-4 text-[11px] font-normal text-muted-foreground">
              账号
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              活动
            </TableHead>
            <TableHead className="text-[11px] font-normal text-muted-foreground">
              兑换码
            </TableHead>
            <TableHead className="pr-4 text-right text-[11px] font-normal text-muted-foreground">
              操作
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {codes.map((row, i) => {
            const key = `${row.account || ''}:${row.campaign_id || row.campaign || ''}:${i}`;
            const realm = row.realm === 'intl' ? 'intl' : row.realm === 'cn' ? 'cn' : '';
            return (
              <TableRow key={key} className="border-b border-border/40">
                <TableCell className="pl-4 align-top">
                  <div className="text-sm font-medium">
                    {row.nickname || uid8(row.account || '')}
                  </div>
                  {row.account && (
                    <div className="flex items-center gap-1.5">
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {uid8(row.account)}
                      </span>
                      {realm && (
                        <Badge
                          variant="outline"
                          className={cn('rounded-full text-[10px]', REALM_BADGE[realm])}
                        >
                          {realm === 'cn' ? '国内版' : '国际版'}
                        </Badge>
                      )}
                    </div>
                  )}
                </TableCell>
                <TableCell className="align-top">
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {row.campaign || row.campaign_id || '-'}
                  </span>
                </TableCell>
                <TableCell className="align-top">
                  <span className="font-mono text-[13px] tracking-[0.5px]">
                    {row.code || '-'}
                  </span>
                </TableCell>
                <TableCell className="pr-4 text-right align-top">
                  <div className="flex items-center justify-end gap-1.5">
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 rounded-full text-xs"
                      disabled={busyKey !== ''}
                      title="复制兑换码"
                      onClick={() => void onCopy(row, key)}
                    >
                      {busyKey === key ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : (
                        <Copy className="size-3.5" />
                      )}
                      {copiedKey === key ? '已复制' : '复制'}
                    </Button>
                    {row.url ? (
                      <Button
                        asChild
                        variant="outline"
                        size="sm"
                        className="h-7 rounded-full text-xs"
                      >
                        <a
                          href={row.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          title="打开官方活动页（同一账号可查看二维码）"
                        >
                          <ExternalLink className="size-3.5" />
                          活动页/二维码
                        </a>
                      </Button>
                    ) : null}
                  </div>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>

      <div className="px-4 pb-3 pt-2 text-[11px] text-muted-foreground">
        兑换码仅保存在本机账号文件里；「活动页/二维码」打开官方活动页可用同一账号查看二维码。
      </div>
    </section>
  );
}
