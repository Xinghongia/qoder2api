'use client';

import * as React from 'react';
import {Loader2, RefreshCw} from 'lucide-react';

import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {api} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';

/**
 * 本机凭证扫描导入（旧看板 dashboard.html 的 desktopScanModal 行为基准）。
 *
 * 后端契约（qoder2api/api/accounts_routes.py 的 /accounts/import/desktop 分支）：
 *   · POST { }              → 只读扫描，返回 {detected, accounts, pool_uids}
 *   · POST {path, realm}    → 按候选导入单个凭证
 *   · POST {all: true}      → 导入全部有效项（本弹窗未使用，行为对齐旧看板：
 *                             逐个「导入」按钮）
 * detected 项字段见 qoder2api/accounts.py 的 scan_desktop_credentials()：
 *   kind / path / file / realm / realmName / domain / readable / valid /
 *   uid / nickname / expiresAt / expiresIn / error
 *
 * 全过程只读检测；点「导入」才入池。导入后原地刷新账号表，不切换区域。
 */

interface DesktopCredential {
  kind: string;
  path: string;
  file: string;
  realm: string;
  realmName?: string;
  domain?: string;
  readable?: boolean;
  valid?: boolean;
  uid?: string;
  nickname?: string;
  expiresAt?: number;
  expiresIn?: string | null;
  error?: string;
}

interface DesktopResponse {
  detected?: DesktopCredential[];
  pool_uids?: string[];
  imported?: Array<{uid?: string; nickname?: string; realm?: string}>;
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const uid8 = (uid?: string) => (uid || '').slice(0, 8);
const realmLabel = (realm: string) => (realm === 'cn' ? '国内版' : '国际版');

function CredentialRow({
  item,
  imported,
  importing,
  onImport,
}: {
  item: DesktopCredential;
  imported: boolean;
  importing: boolean;
  onImport: (item: DesktopCredential) => void;
}) {
  const name = item.nickname || uid8(item.uid) || item.file;
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-border/60 bg-background px-3 py-2">
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-sm font-medium">{name}</span>
          <Badge variant="secondary" className="shrink-0 rounded-full text-[10px]">
            {item.kind === 'app' ? '桌面 App' : 'CLI'}
          </Badge>
        </div>
        <div className="truncate font-mono text-[10px] text-muted-foreground">
          {uid8(item.uid) || '—'} · {item.file}
          {item.domain ? ` · ${item.domain}` : ''}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="hidden text-[11px] text-muted-foreground sm:inline">
          {item.expiresIn || '有效期未知'}
        </span>
        {imported ? (
          <Badge
            variant="outline"
            className={cn(
              'rounded-full text-[10px]',
              'border-emerald-600/30 bg-emerald-600/10 text-emerald-700 dark:text-emerald-400',
            )}
          >
            已导入
          </Badge>
        ) : (
          <Button
            type="button"
            size="sm"
            className="h-7 rounded-full px-3 text-xs"
            disabled={importing}
            onClick={() => onImport(item)}
          >
            {importing && <Loader2 className="size-3.5 animate-spin" />}
            导入
          </Button>
        )}
      </div>
    </div>
  );
}

export function DesktopScanDialog({
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
  const viewRef = React.useRef(view);
  viewRef.current = view;

  const [items, setItems] = React.useState<DesktopCredential[]>([]);
  const [pool, setPool] = React.useState<Set<string>>(new Set());
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const [importing, setImporting] = React.useState('');
  const genRef = React.useRef(0);

  const scan = React.useCallback(async () => {
    const gen = ++genRef.current;
    setLoading(true);
    setError('');
    try {
      const r = (await api.accounts.importDesktop({})) as DesktopResponse;
      if (genRef.current !== gen) return;
      setItems(r.detected || []);
      setPool(new Set(r.pool_uids || []));
    } catch (e) {
      if (genRef.current !== gen) return;
      setItems([]);
      setError(errText(e));
    } finally {
      if (genRef.current === gen) setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    if (!open) return;
    void scan();
    return () => {
      genRef.current += 1;
    };
  }, [open, scan]);

  const importOne = async (item: DesktopCredential) => {
    if (importing) return;
    setImporting(item.path);
    try {
      const r = (await api.accounts.importDesktop({
        path: item.path,
        realm: item.realm,
      })) as DesktopResponse;
      const a = (r.imported || [])[0] || {};
      const label = a.nickname || uid8(a.uid) || item.nickname || uid8(item.uid) || item.file;
      notify.ok('已导入本机凭证', `${label} · ${realmLabel(item.realm)}`);
      if (item.realm !== viewRef.current) {
        notify.info('该账号属于另一区域', `在${realmLabel(item.realm)}列表中查看，当前视图未切换`);
      }
      await onChanged();
      await scan(); // 刷新候选列表，标出「已导入」
    } catch (e) {
      notify.err('导入失败', errText(e));
    } finally {
      setImporting('');
    }
  };

  const valid = items.filter((d) => d.valid);
  const bad = items.filter(
    (d) => !d.valid && !!d.error && !d.error.startsWith('not found'),
  );
  const absent = items.filter(
    (d) => !d.valid && (!d.error || d.error.startsWith('not found')),
  );
  const intl = valid.filter((d) => d.realm !== 'cn');
  const cn = valid.filter((d) => d.realm === 'cn');

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[720px]">
        <DialogHeader>
          <DialogTitle>本机已登录的 Qoder 凭证</DialogTitle>
          <DialogDescription>
            只读检测本机桌面 App 与命令行客户端里已登录的凭证（国际版 / 国内版），
            点击「导入」才会加入网关。导入后可删除原客户端或继续共用。
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="max-h-[min(64vh,560px)]">
          <div className="space-y-4 px-6 pb-2">
            {loading && (
              <div className="flex items-center gap-2 rounded-xl border border-border/60 bg-muted/40 px-3 py-3 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" />
                正在只读检测本机已登录的 Qoder 凭证…
              </div>
            )}

            {!loading && error && (
              <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-3 text-xs text-amber-700 dark:text-amber-400">
                <p className="font-medium">扫描失败</p>
                <p className="mt-1 break-all">{error}</p>
              </div>
            )}

            {!loading && !error && valid.length === 0 && (
              <p className="text-xs leading-5 text-muted-foreground">
                未在本机检测到已登录的 Qoder 凭证。
                <br />
                请确认已登录 Qoder 桌面 App（国际版 / 国内版）或 Qoder CLI 后点「重新扫描」，
                也可以用「OAuth 设备授权」或「导入 PAT」添加账号。
              </p>
            )}

            {!loading && !error && valid.length > 0 && (
              <>
                <section className="space-y-2">
                  <h3 className="text-xs font-semibold">
                    国际版 (Global){' '}
                    <span className="font-normal text-muted-foreground">
                      qoder.com ({intl.length})
                    </span>
                  </h3>
                  {intl.length === 0 ? (
                    <p className="text-xs text-muted-foreground">该区域未检测到已登录凭证</p>
                  ) : (
                    intl.map((d) => (
                      <CredentialRow
                        key={d.path}
                        item={d}
                        imported={!!d.uid && pool.has(d.uid)}
                        importing={importing === d.path}
                        onImport={(item) => void importOne(item)}
                      />
                    ))
                  )}
                </section>

                <section className="space-y-2">
                  <h3 className="text-xs font-semibold">
                    国内版 (China){' '}
                    <span className="font-normal text-muted-foreground">
                      qoder.com.cn ({cn.length})
                    </span>
                  </h3>
                  {cn.length === 0 ? (
                    <p className="text-xs text-muted-foreground">该区域未检测到已登录凭证</p>
                  ) : (
                    cn.map((d) => (
                      <CredentialRow
                        key={d.path}
                        item={d}
                        imported={!!d.uid && pool.has(d.uid)}
                        importing={importing === d.path}
                        onImport={(item) => void importOne(item)}
                      />
                    ))
                  )}
                </section>
              </>
            )}

            {!loading && !error && bad.length > 0 && (
              <section className="space-y-2">
                <h3 className="text-xs font-semibold text-amber-600 dark:text-amber-400">
                  读取失败的凭证
                </h3>
                {bad.map((d) => (
                  <div
                    key={d.path}
                    className="flex items-center justify-between gap-3 rounded-xl border border-border/60 px-3 py-2"
                  >
                    <span className="truncate font-mono text-[11px] text-muted-foreground">
                      {d.file}
                    </span>
                    <span className="max-w-[60%] truncate text-[11px] text-amber-600 dark:text-amber-400">
                      {d.error || '未知原因'}
                    </span>
                  </div>
                ))}
              </section>
            )}

            {!loading && !error && absent.length > 0 && (
              <p className="text-[11px] leading-4 text-muted-foreground">
                未检测到登录的位置：
                {absent.map((d) => `${d.realmName || realmLabel(d.realm)}·${d.file}`).join('，')}
              </p>
            )}
          </div>
        </DialogBody>

        <DialogFooter>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="rounded-full"
            disabled={loading}
            onClick={() => void scan()}
          >
            {loading ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <RefreshCw className="size-3.5" />
            )}
            重新扫描
          </Button>
          <Button
            type="button"
            size="sm"
            variant="secondary"
            className="rounded-full"
            onClick={() => onOpenChange(false)}
          >
            关闭
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
