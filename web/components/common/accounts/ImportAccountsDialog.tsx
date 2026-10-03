'use client';

import * as React from 'react';
import {FileJson, FileUp, Loader2} from 'lucide-react';

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
import {Label} from '@/components/ui/label';
import {Textarea} from '@/components/ui/textarea';
import {api} from '@/lib/api';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';

/**
 * JSON 导入（旧看板 dashboard.html 的 importModal 行为基准，另补粘贴入口）。
 *
 * 后端契约（qoder2api/api/accounts_routes.py 的 /accounts/import 分支）：
 *   POST {data, dryRun: true, overwrite?} → 预检：{dryRun, count, result:{added,
 *        updated, skipped, invalid}, accounts}，不落盘；
 *   POST {data, overwrite?}              → 正式导入：{count, result, accounts}
 *   data 支持：账号数组 / 本网关导出文档（accounts 数组）/ 单个账号对象
 *   （qoder2api/accounts.py 的 _coerce_account_rows）。overwrite=true 覆盖同 UID。
 *
 * 预检先行，用户看清新增 / 覆盖 / 跳过 / 无效后才确认；成功后原地刷新账号表。
 */

interface ImportBucketItem {
  uid?: string;
  index?: number;
  reason?: string;
}

interface ImportReport {
  added?: string[];
  updated?: string[];
  skipped?: ImportBucketItem[];
  invalid?: ImportBucketItem[];
}

interface ImportResponse {
  dryRun?: boolean;
  count?: number;
  result?: ImportReport;
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** 客户端粗校验：后端 _coerce_account_rows 认得的三种文档形态。 */
function looksLikeImport(doc: unknown): boolean {
  if (Array.isArray(doc)) return doc.length > 0;
  if (doc && typeof doc === 'object') {
    const d = doc as Record<string, unknown>;
    if (Array.isArray(d.accounts)) return d.accounts.length > 0;
    return Boolean(d.accessToken || d.auth || d.account);
  }
  return false;
}

function Bucket({
  title,
  tone,
  items,
}: {
  title: string;
  tone: string;
  items: Array<string | ImportBucketItem>;
}) {
  const shown = items.slice(0, 12);
  return (
    <div>
      <div className={cn('text-xs font-medium', tone)}>
        {title}（{items.length}）
      </div>
      <div className="mt-1 flex flex-wrap gap-1">
        {shown.map((x, i) => {
          const uid =
            typeof x === 'string' ? x : x.uid || (x.index ? `#${x.index}` : '?');
          const reason = typeof x === 'string' ? '' : x.reason || '';
          return (
            <span
              key={`${uid}-${i}`}
              title={reason || undefined}
              className="max-w-full truncate rounded-full border border-border/60 bg-background px-2 py-0.5 font-mono text-[10px] text-muted-foreground"
            >
              {uid.slice(0, 20)}
              {reason ? ` · ${reason}` : ''}
            </span>
          );
        })}
        {items.length > shown.length && (
          <span className="rounded-full px-2 py-0.5 text-[10px] text-muted-foreground">
            …等 {items.length} 个
          </span>
        )}
      </div>
    </div>
  );
}

export function ImportAccountsDialog({
  open,
  onOpenChange,
  onChanged,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 成功后原地刷新账号表（不得切换区域 / 视图）。 */
  onChanged: () => Promise<void> | void;
}) {
  const [doc, setDoc] = React.useState<unknown>(null);
  const [rawText, setRawText] = React.useState('');
  const [filename, setFilename] = React.useState('');
  const [pasted, setPasted] = React.useState('');
  const [preview, setPreview] = React.useState<ImportReport | null>(null);
  const [total, setTotal] = React.useState(0);
  const [overwrite, setOverwrite] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');
  const fileRef = React.useRef<HTMLInputElement | null>(null);
  const genRef = React.useRef(0);

  React.useEffect(() => {
    if (!open) return;
    genRef.current += 1;
    setDoc(null);
    setRawText('');
    setFilename('');
    setPasted('');
    setPreview(null);
    setTotal(0);
    setOverwrite(false);
    setBusy(false);
    setError('');
  }, [open]);

  /** 解析 + dry-run 预检；文案与旧看板一致：先看清结果再确认。 */
  const analyze = React.useCallback(async (text: string, name: string, ow: boolean) => {
    const gen = ++genRef.current;
    setBusy(true);
    setError('');
    setPreview(null);
    setDoc(null);
    setFilename(name);
    try {
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new Error('内容不是合法 JSON');
      }
      if (!looksLikeImport(parsed)) {
        throw new Error(
          '没有找到账号：需要非空的账号数组、带 accounts 数组的导出文件，或单个账号对象',
        );
      }
      const r = (await api.accounts.importJSON({
        data: parsed,
        dryRun: true,
        overwrite: ow,
      })) as ImportResponse;
      if (genRef.current !== gen) return;
      setRawText(text);
      setDoc(parsed);
      setTotal(r.count ?? 0);
      setPreview(r.result || {added: [], updated: [], skipped: [], invalid: []});
    } catch (e) {
      if (genRef.current !== gen) return;
      setError(errText(e));
    } finally {
      if (genRef.current === gen) setBusy(false);
    }
  }, []);

  const onPickFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = ''; // 允许再次选择同一个文件
    if (!f) return;
    try {
      const text = await f.text();
      await analyze(text, f.name, overwrite);
    } catch (err) {
      setError(errText(err));
    }
  };

  /** 覆盖开关会改变预检结果（已存在账号从「跳过」变「覆盖」），重新预检。 */
  const toggleOverwrite = (next: boolean) => {
    setOverwrite(next);
    if (rawText) void analyze(rawText, filename, next);
  };

  const importable = (preview?.added?.length || 0) + (preview?.updated?.length || 0);

  const commit = async () => {
    if (!doc || busy || importable === 0) return;
    setBusy(true);
    try {
      const r = (await api.accounts.importJSON({data: doc, overwrite})) as ImportResponse;
      const res = r.result || {};
      const added = res.added?.length || 0;
      const updated = res.updated?.length || 0;
      const skipped = res.skipped?.length || 0;
      const invalid = res.invalid?.length || 0;
      notify.ok('导入完成', `新增 ${added} · 覆盖 ${updated} · 跳过 ${skipped} · 无效 ${invalid}`);
      await onChanged();
      if (added + updated > 0) {
        onOpenChange(false);
      } else {
        setPreview(res);
      }
    } catch (e) {
      notify.err('导入失败', errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[680px]">
        <DialogHeader>
          <DialogTitle>导入账号（JSON）</DialogTitle>
          <DialogDescription>
            选择账号 JSON 文件或直接粘贴内容。支持本网关导出的文件、账号数组、单个账号对象
            （accessToken / auth）；区域按文档内容自动识别。
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="max-h-[min(64vh,560px)]">
          <div className="space-y-4 px-6 pb-2">
            <div className="flex flex-wrap items-center gap-2">
              <input
                ref={fileRef}
                type="file"
                accept=".json,application/json"
                className="hidden"
                onChange={(e) => void onPickFile(e)}
              />
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="rounded-full"
                disabled={busy}
                onClick={() => fileRef.current?.click()}
              >
                <FileUp className="size-3.5" />
                选择 JSON 文件
              </Button>
              {filename && (
                <span className="truncate text-xs text-muted-foreground">
                  当前：{filename} · 共 {total} 条
                </span>
              )}
            </div>

            <div className="space-y-2">
              <span className="text-xs text-muted-foreground">
                或粘贴 JSON 内容（对象、数组或导出的文件内容）：
              </span>
              <Textarea
                value={pasted}
                rows={5}
                placeholder='例如 [{"accessToken":"dt-…","uid":"…","realm":"cn"}]'
                onChange={(e) => setPasted(e.target.value)}
              />
              <Button
                type="button"
                size="sm"
                variant="secondary"
                className="rounded-full"
                disabled={busy || !pasted.trim()}
                onClick={() => void analyze(pasted, '粘贴的 JSON', overwrite)}
              >
                {busy ? <Loader2 className="size-3.5 animate-spin" /> : <FileJson className="size-3.5" />}
                解析并预检
              </Button>
            </div>

            {error && (
              <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
                {error}
              </div>
            )}

            {busy && !preview && (
              <p className="flex items-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" />
                正在预检文档…
              </p>
            )}

            {preview && (
              <div className="space-y-3 rounded-xl border border-border/60 bg-muted/40 p-3">
                <div className="flex flex-wrap gap-3 text-xs">
                  <span>
                    新增{' '}
                    <b className="text-emerald-600 dark:text-emerald-400">
                      {preview.added?.length || 0}
                    </b>
                  </span>
                  <span>
                    覆盖 <b>{preview.updated?.length || 0}</b>
                  </span>
                  <span>
                    跳过 <b>{preview.skipped?.length || 0}</b>
                  </span>
                  <span>
                    无效{' '}
                    <b className="text-amber-600 dark:text-amber-400">
                      {preview.invalid?.length || 0}
                    </b>
                  </span>
                </div>

                {(preview.added?.length || 0) > 0 && (
                  <Bucket
                    title="将新增"
                    tone="text-emerald-600 dark:text-emerald-400"
                    items={preview.added || []}
                  />
                )}
                {(preview.updated?.length || 0) > 0 && (
                  <Bucket title="将覆盖" tone="text-foreground" items={preview.updated || []} />
                )}
                {(preview.skipped?.length || 0) > 0 && (
                  <Bucket
                    title="将跳过"
                    tone="text-muted-foreground"
                    items={preview.skipped || []}
                  />
                )}
                {(preview.invalid?.length || 0) > 0 && (
                  <Bucket
                    title="无法解析"
                    tone="text-amber-600 dark:text-amber-400"
                    items={preview.invalid || []}
                  />
                )}

                {importable === 0 && (
                  <p className="text-xs text-amber-600 dark:text-amber-400">
                    没有可导入的账号。
                    {(preview.skipped?.length || 0) > 0 &&
                      '若要覆盖已存在的账号，请勾选下方「覆盖同 UID 账号」后重新预检。'}
                  </p>
                )}
              </div>
            )}

            <Label className="items-start gap-2 text-xs font-normal text-muted-foreground">
              <input
                type="checkbox"
                className="mt-0.5 size-4 accent-primary"
                checked={overwrite}
                disabled={busy}
                onChange={(e) => toggleOverwrite(e.target.checked)}
              />
              <span>
                覆盖同 UID 账号
                {overwrite && (
                  <span className="ml-1 text-amber-600 dark:text-amber-400">
                    （将覆盖同 UID 现有账号的凭证，操作前已重新预检）
                  </span>
                )}
              </span>
            </Label>
          </div>
        </DialogBody>

        <DialogFooter>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="rounded-full"
            disabled={busy}
            onClick={() => onOpenChange(false)}
          >
            取消
          </Button>
          <Button
            type="button"
            size="sm"
            className="rounded-full"
            disabled={busy || !doc || importable === 0}
            onClick={() => void commit()}
          >
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {importable > 0
              ? overwrite
                ? `确认覆盖并导入 ${importable} 个`
                : `确认导入 ${importable} 个`
              : '确认导入'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
