'use client';

import * as React from 'react';
import {KeyRound, Loader2} from 'lucide-react';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {api, type Realm} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';

/**
 * PAT 导入（旧看板 dashboard.html 的 patModal 行为基准）。
 *
 * 后端契约（qoder2api/api/accounts_routes.py 的 /accounts/import/pat 分支）：
 *   POST /accounts/import/pat {pat, realm} → {imported: [account], accounts}
 *   pat 必须以 "pt-" 开头（后端 qoder2api/accounts.py import_pat 会再次校验）。
 *
 * 成功后原地刷新账号表并自动关窗，不切换区域。
 */

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const uid8 = (uid?: string) => (uid || '').slice(0, 8);

interface PatResponse {
  imported?: Array<{uid?: string; nickname?: string}>;
}

export function PatImportDialog({
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

  const [pat, setPat] = React.useState('');
  const [realm, setRealm] = React.useState<Realm>(view);
  const [busy, setBusy] = React.useState(false);
  const [hint, setHint] = React.useState('');
  const [done, setDone] = React.useState('');
  const closeTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    if (!open) return;
    setPat('');
    setRealm(viewRef.current);
    setBusy(false);
    setHint('');
    setDone('');
    return () => {
      if (closeTimer.current) {
        clearTimeout(closeTimer.current);
        closeTimer.current = null;
      }
    };
    // 只在开窗时重置；区域默认跟随当前视图
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const submit = async () => {
    if (busy) return;
    const value = pat.trim();
    if (!value.startsWith('pt-')) {
      setHint('令牌必须以 pt- 开头，请粘贴完整的 Personal Access Token。');
      return;
    }
    setBusy(true);
    setHint('');
    setDone('');
    try {
      const r = (await api.accounts.importPat({pat: value, realm})) as PatResponse;
      const a = (r.imported || [])[0] || {};
      const label = a.nickname || uid8(a.uid) || '新账号';
      setPat('');
      setDone(`已导入 ${label} 到${realm === 'cn' ? '国内版' : '国际版'}`);
      notify.ok('PAT 已导入', `${label} · ${realm === 'cn' ? '国内版' : '国际版'}`);
      await onChanged();
      closeTimer.current = setTimeout(() => onOpenChange(false), 1200);
    } catch (e) {
      setHint(`导入失败：${errText(e)}`);
      notify.err('PAT 导入失败', errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[560px]">
        <DialogHeader>
          <DialogTitle>导入 PAT（Personal Access Token）</DialogTitle>
          <DialogDescription>
            在 Qoder 网页版「设置 → Personal Access Token」创建 pt- 开头的令牌，
            粘贴到下方并选择所属区域。网关会自动交换 jobToken 并拉取账号身份入池。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 px-6 pb-2">
          <Input
            type="password"
            value={pat}
            autoComplete="off"
            placeholder="pt- 开头的 Personal Access Token"
            onChange={(e) => {
              setPat(e.target.value);
              setHint('');
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void submit();
              }
            }}
          />

          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-muted-foreground">令牌所属区域</span>
            <div className="flex flex-wrap gap-1">
              <Button
                type="button"
                size="sm"
                variant={realm === 'cn' ? 'secondary' : 'ghost'}
                className="h-7 rounded-full text-xs"
                disabled={busy}
                onClick={() => setRealm('cn')}
              >
                国内版 (qoder.com.cn)
              </Button>
              <Button
                type="button"
                size="sm"
                variant={realm === 'intl' ? 'secondary' : 'ghost'}
                className="h-7 rounded-full text-xs"
                disabled={busy}
                onClick={() => setRealm('intl')}
              >
                国际版 (qoder.com)
              </Button>
            </div>
          </div>

          {hint && (
            <p className="break-all text-xs text-amber-600 dark:text-amber-400">{hint}</p>
          )}
          {done && (
            <p className="text-xs text-emerald-600 dark:text-emerald-400">{done}</p>
          )}
          {busy && (
            <p className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" />
              正在交换 jobToken 并拉取账号身份…
            </p>
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="rounded-full"
            disabled={busy}
            onClick={() => onOpenChange(false)}
          >
            关闭
          </Button>
          <Button
            type="button"
            size="sm"
            className="rounded-full"
            disabled={busy || !pat.trim()}
            onClick={() => void submit()}
          >
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <KeyRound className="size-3.5" />}
            导入 PAT
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
