'use client';

import * as React from 'react';
import {useRouter} from 'next/navigation';
import {KeyRound, Loader2, LogIn, ShieldCheck} from 'lucide-react';
import {motion} from 'motion/react';

import {useAuth} from '@/lib/auth-context';
import {notify} from '@/lib/toast';

/**
 * 面板登录页（独立整页，结构对齐 workbuddy-manager 的登录页）：
 * 居中卡片 + 账号 / 密码双栏 + 圆角胶囊按钮；未登录访问任何 (main) 页面
 * 都会被重定向到这里，登录成功后回到仪表盘。
 *
 * 表单值直接从 DOM 读取：浏览器自动填充经常不触发 React onChange，
 * 读 state 会拿到空串（workbuddy 踩过的坑）。
 */
export default function LoginPage() {
  const {ready, needsLogin, passwordIsDefault, login} = useAuth();
  const router = useRouter();
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');

  // 已登录（或从缓存恢复的会话）直接进仪表盘
  React.useEffect(() => {
    if (ready && !needsLogin) router.replace('/');
  }, [ready, needsLogin, router]);

  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (busy) return;
    const form = e.currentTarget;
    const username = (form.elements.namedItem('username') as HTMLInputElement)?.value.trim();
    const password = (form.elements.namedItem('password') as HTMLInputElement)?.value;
    if (!username) {
      setError('请输入账号');
      return;
    }
    if (!password) {
      setError('请输入密码');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await login(username, password);
      notify.ok('已登录', '欢迎回来');
      router.replace('/');
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-background relative flex min-h-svh flex-col items-center justify-center gap-6 p-6 md:p-10">
      <motion.div
        initial={{opacity: 0, y: 12}}
        animate={{opacity: 1, y: 0}}
        transition={{duration: 0.35}}
        className="w-full max-w-sm"
      >
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid h-11 w-11 place-items-center rounded-2xl bg-primary text-primary-foreground">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-lg font-semibold tracking-[-0.01em]">Qoder 网关看板</h1>
            <p className="mt-1 text-xs text-muted-foreground">
              登录后可管理账号池、密钥与出口
            </p>
          </div>
        </div>

        <form onSubmit={submit} className="rounded-[24px] bg-muted p-5">
          <div className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="username" className="text-[11px] text-muted-foreground">
                账号
              </label>
              <input
                id="username"
                name="username"
                autoComplete="username"
                placeholder="admin"
                defaultValue="admin"
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="password" className="text-[11px] text-muted-foreground">
                密码
              </label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                autoFocus
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            {error && <p className="text-xs text-red-500">{error}</p>}
            <button
              type="submit"
              disabled={busy}
              className="inline-flex h-9 w-full items-center justify-center gap-2 rounded-full bg-primary text-sm font-medium text-primary-foreground transition-opacity disabled:opacity-50"
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
              {busy ? '登录中…' : '登录'}
            </button>
          </div>
        </form>

        {passwordIsDefault && (
          <p className="mt-4 flex items-start justify-center gap-1.5 text-center text-[11px] text-amber-600 dark:text-amber-400">
            <KeyRound className="mt-px h-3 w-3 shrink-0" />
            当前仍是默认密码 admin，登录后请到「设置」修改
          </p>
        )}
        <p className="mt-6 text-center text-[11px] text-muted-foreground">
          面板密码与登录账号保存在 accounts/settings.json，忘记时清空该文件里的
          panel_password_* 与 panel_username 字段即可恢复默认。
        </p>
      </motion.div>
    </div>
  );
}
