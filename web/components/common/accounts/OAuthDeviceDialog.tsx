'use client';

import * as React from 'react';
import {Check, Copy, ExternalLink, Loader2, RefreshCw} from 'lucide-react';

import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Progress} from '@/components/ui/progress';
import {api, type Realm} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';

/**
 * OAuth 设备授权登录（旧看板 dashboard.html 的 loginModal 行为基准）。
 *
 * 后端契约（qoder2api/api/accounts_routes.py + qoder2api/accounts.py）：
 *   · POST /accounts/login/start {realm, platform} → {state, authUrl, realm, platform}
 *     —— 返回的是一段浏览器授权链接（device/selectAccounts），没有单独的 user_code 字段；
 *   · GET  /accounts/login/poll?state= → {status: pending|ok|expired|unknown|error, message?, account?}
 *   · POST /accounts/login/cancel {state} → {cancelled}
 *
 * 轮询在关窗 / 卸载时停止并取消未完成的登录；成功后自动入池、自动关窗，
 * 只通过 onChanged() 原地刷新账号表，不切换页面区域。
 */

const POLL_MS = 2500;
const TTL_SECONDS = 600; // 后端 LOGIN_TTL_SECONDS

type Phase = 'idle' | 'starting' | 'waiting' | 'success' | 'error';

interface LoginPollResult {
  status: string;
  message?: string;
  account?: {uid?: string; nickname?: string};
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const uid8 = (uid?: string) => (uid || '').slice(0, 8);

export function OAuthDeviceDialog({
  open,
  onOpenChange,
  onChanged,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 成功后原地刷新账号表（不得切换区域 / 视图）。 */
  onChanged: () => Promise<void> | void;
}) {
  const {view} = useRealm();
  const viewRef = React.useRef<Realm>(view);
  viewRef.current = view;

  const [realm, setRealm] = React.useState<Realm>(view);
  const [phase, setPhase] = React.useState<Phase>('idle');
  const [authUrl, setAuthUrl] = React.useState('');
  const [statusText, setStatusText] = React.useState('');
  const [error, setError] = React.useState('');
  const [elapsed, setElapsed] = React.useState(0);
  const [copied, setCopied] = React.useState(false);

  const stateRef = React.useRef('');
  const genRef = React.useRef(0);
  const pollTimer = React.useRef<ReturnType<typeof setInterval> | null>(null);
  const tickTimer = React.useRef<ReturnType<typeof setInterval> | null>(null);
  const closeTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopTimers = React.useCallback(() => {
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
    if (tickTimer.current) {
      clearInterval(tickTimer.current);
      tickTimer.current = null;
    }
  }, []);

  const clearCloseTimer = React.useCallback(() => {
    if (closeTimer.current) {
      clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);

  const cancelState = React.useCallback((state: string) => {
    if (!state) return;
    api.accounts.loginCancel(state).catch(() => undefined);
  }, []);

  const poll = React.useCallback(async () => {
    const current = stateRef.current;
    if (!current) return;
    try {
      const r = (await api.accounts.loginPoll(current)) as LoginPollResult;
      if (stateRef.current !== current) return; // 已被取消 / 重新发起
      if (r.status === 'ok') {
        stopTimers();
        clearCloseTimer();
        stateRef.current = '';
        const name = r.account?.nickname || uid8(r.account?.uid) || '新账号';
        setPhase('success');
        setStatusText(`登录成功：${name} 已加入账号池`);
        notify.ok('账号已添加', `${name} 已加入账号池`);
        await onChanged();
        closeTimer.current = setTimeout(() => onOpenChange(false), 1200);
      } else if (r.status === 'pending') {
        setStatusText(r.message || '等待浏览器完成 Qoder 设备授权…');
      } else {
        stopTimers();
        stateRef.current = '';
        setPhase('error');
        setStatusText('');
        setError(r.message || `授权已中断（${r.status}）`);
      }
    } catch (e) {
      // 网络抖动不结束轮询；state 失效等语义错误由后端以 status 返回
      setStatusText(`检测暂时失败，正在重试…（${errText(e)}）`);
    }
  }, [clearCloseTimer, onChanged, onOpenChange, stopTimers]);

  const start = React.useCallback(
    async (target: Realm) => {
      clearCloseTimer();
      stopTimers();
      const prev = stateRef.current;
      stateRef.current = '';
      cancelState(prev);
      genRef.current += 1;
      const gen = genRef.current;

      setPhase('starting');
      setError('');
      setCopied(false);
      setElapsed(0);
      // 刻意**不清空 authUrl**：切换区域时把上一区的链接先留着（按钮禁用、
      // 状态行提示「正在切换」），等新链接到达再替换。若在这里清空，链接区块
      // （约 120px 高）会先塌陷再展开，看起来就是「闪一下」。
      setStatusText(
        target === 'cn'
          ? '正在向 qoder.com.cn（国内版）申请设备授权…'
          : '正在向 qoder.com（国际版）申请设备授权…',
      );

      try {
        const r = await api.accounts.loginStart(target);
        if (genRef.current !== gen) {
          // 等待响应期间被关窗 / 重开：把服务端刚建的 state 取消掉
          cancelState(r.state);
          return;
        }
        stateRef.current = r.state;
        setAuthUrl(r.authUrl);
        setPhase('waiting');
        setStatusText('等待浏览器完成 Qoder 设备授权…');
        const startedAt = Date.now();
        tickTimer.current = setInterval(() => {
          setElapsed(Math.floor((Date.now() - startedAt) / 1000));
        }, 1000);
        pollTimer.current = setInterval(() => {
          void poll();
        }, POLL_MS);
        void poll();
      } catch (e) {
        if (genRef.current !== gen) return;
        setPhase('error');
        setStatusText('');
        setError(errText(e));
      }
    },
    [cancelState, clearCloseTimer, poll, stopTimers],
  );

  React.useEffect(() => {
    if (!open) return;
    // 每次**开窗**都从干净状态开始（切换区域走 chooseRealm，不清链接）
    setAuthUrl('');
    setElapsed(0);
    setError('');
    setPhase('idle');
    setRealm(viewRef.current); // 每次开窗跟随当前视图区域
    void start(viewRef.current);
    return () => {
      genRef.current += 1;
      stopTimers();
      clearCloseTimer();
      const pending = stateRef.current;
      stateRef.current = '';
      cancelState(pending);
    };
    // 只在开窗时发起一次；弹窗内切换区域由 chooseRealm 重新发起
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const chooseRealm = (next: Realm) => {
    if (next === realm && (phase === 'starting' || phase === 'waiting')) return;
    setRealm(next);
    void start(next);
  };

  const copyLink = async () => {
    if (!authUrl) return;
    try {
      await navigator.clipboard.writeText(authUrl);
      setCopied(true);
      notify.ok('授权链接已复制', '在浏览器打开并完成登录授权');
      setTimeout(() => setCopied(false), 1500);
    } catch {
      notify.err('复制失败', '请手动选中链接复制');
    }
  };

  const busy = phase === 'starting' || phase === 'waiting';
  const progress =
    phase === 'waiting' ? Math.min(96, Math.round((elapsed / TTL_SECONDS) * 100)) : 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[520px]">
        <DialogHeader>
          <DialogTitle>添加账号（OAuth 设备授权）</DialogTitle>
          <DialogDescription>
            选择版本后，在浏览器打开授权链接并完成登录；本窗口会自动检测并入池。
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="max-h-[min(62vh,540px)]">
          <div className="space-y-4 px-6 pb-2">
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium text-muted-foreground">选择要登录的版本</span>
              <div className="flex flex-wrap gap-1">
                <Button
                  type="button"
                  size="sm"
                  variant={realm === 'cn' ? 'secondary' : 'ghost'}
                  className="h-7 rounded-full text-xs"
                  disabled={phase === 'starting'}
                  onClick={() => chooseRealm('cn')}
                >
                  国内版 (qoder.com.cn)
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={realm === 'intl' ? 'secondary' : 'ghost'}
                  className="h-7 rounded-full text-xs"
                  disabled={phase === 'starting'}
                  onClick={() => chooseRealm('intl')}
                >
                  国际版 (qoder.com)
                </Button>
              </div>
            </div>

            <div className="rounded-xl border border-border/60 bg-muted/40 p-3">
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                {busy && <Loader2 className="size-3.5 shrink-0 animate-spin" />}
                <span className="min-w-0 break-all">{statusText}</span>
              </div>

              {authUrl && (
                <div className={cn('mt-3 space-y-2', busy && 'pointer-events-none opacity-60')}>
                  <div className="flex items-center gap-2">
                    <Input
                      readOnly
                      value={authUrl}
                      className="h-8 font-mono text-[11px]"
                      onFocus={(e) => e.currentTarget.select()}
                    />
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      className="h-8 shrink-0 rounded-full"
                      disabled={busy}
                      onClick={() => void copyLink()}
                    >
                      {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                      {copied ? '已复制' : '复制'}
                    </Button>
                  </div>
                  <Button
                    asChild
                    size="sm"
                    variant="outline"
                    className="h-8 rounded-full"
                    aria-disabled={busy}
                  >
                    <a href={authUrl} target="_blank" rel="noopener noreferrer">
                      <ExternalLink className="size-3.5" />
                      在浏览器打开授权链接
                    </a>
                  </Button>
                  {phase === 'waiting' && (
                    <div className="space-y-1.5 pt-1">
                      <Progress value={progress} className="h-1.5" />
                      <p className="text-[11px] text-muted-foreground">
                        已等待 {elapsed} 秒 · 授权链接 10 分钟内有效，完成后自动加入账号池
                      </p>
                    </div>
                  )}
                </div>
              )}

              {error && (
                <p className="mt-2 break-all text-xs text-amber-600 dark:text-amber-400">
                  登录未完成：{error}
                </p>
              )}
              {phase === 'success' && (
                <p className="mt-2 text-xs text-emerald-600 dark:text-emerald-400">
                  已成功加入账号池，窗口即将自动关闭。
                </p>
              )}
            </div>
          </div>
        </DialogBody>

        <DialogFooter>
          {(phase === 'waiting' || phase === 'error') && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="rounded-full"
              onClick={() => void start(realm)}
            >
              <RefreshCw className="size-3.5" />
              重新生成链接
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            variant="secondary"
            className="rounded-full"
            onClick={() => onOpenChange(false)}
          >
            {busy ? '取消并关闭' : '关闭'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
