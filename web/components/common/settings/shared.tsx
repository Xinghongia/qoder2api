'use client';

import * as React from 'react';
import type {ReactNode} from 'react';
import {motion} from 'motion/react';

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
import {cn} from '@/lib/utils';

/**
 * 设置页各分节共用的类型与小组件。
 *
 * 字段名与后端一一对应：GET /settings 的返回见 qoder2api/views.py
 * runtime_settings_view()，POST /settings/save 的入参见
 * qoder2api/api/panel_routes.py _handle_settings_save()。
 */

export interface ApiKeyEntry {
  id: string;
  name: string;
  realm: string;
  enabled: boolean;
  masked: string;
  source: string;
  created_at: string;
}

export interface MachineIdentityView {
  pinned: boolean;
  pinned_at: string;
  bridge_available: boolean;
  effective_source: string;
  preview?: string;
}

export interface ProxyView {
  mode: string;
  url: string;
  effective: string;
  env_override: boolean;
  proxies?: Record<string, string>;
}

export interface SettingsSnapshot {
  panel_password_is_default: boolean;
  /** 面板登录账号（GET /settings 返回，默认 admin）。 */
  panel_username?: string;
  api_key_set: boolean;
  api_key_set_by_panel: boolean;
  api_key_masked: string;
  auth_required: boolean;
  api_keys: ApiKeyEntry[];
  model_overrides: Record<string, unknown>;
  proxy: ProxyView;
  machine_identity: MachineIdentityView;
  accounts_dir: string;
  usage_dir: string;
  settings_file: string;
  version: string;
}

export interface UpdateInfo {
  ok: boolean;
  current: string;
  latest: string;
  has_update: boolean;
  url: string;
  published_at: string;
  name: string;
  checked_at: number;
  error: string;
  no_releases: boolean;
}

export interface IdentityExportResult {
  ok: boolean;
  identity: Record<string, unknown> | null;
  reason: string;
  note: string;
}

export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * 卡片本身是 bg-muted，而 Input / Textarea / SelectTrigger 默认也是
 * bg-muted/55，直接放进去会「融进」卡片里。统一换成与卡片有对比的底色。
 */
export const FIELD_CLASS =
  'bg-background/70 hover:bg-background/90 focus-visible:bg-background/90 dark:bg-background/40 dark:hover:bg-background/50 dark:focus-visible:bg-background/50';

export function FieldLabel({children}: {children: ReactNode}) {
  return (
    <div className="mb-1 text-[11px] font-medium text-muted-foreground">{children}</div>
  );
}

/** 分节卡片：圆角 20px 的 bg-muted 区块，与 StatCard / PageHeader 同一套视觉语言。 */
export function SectionCard({
  title,
  hint,
  aside,
  delay = 0,
  children,
}: {
  title: string;
  hint?: ReactNode;
  aside?: ReactNode;
  delay?: number;
  children: ReactNode;
}) {
  return (
    <motion.section
      initial={{opacity: 0, y: 12}}
      animate={{opacity: 1, y: 0}}
      transition={{duration: 0.35, delay}}
      className="rounded-[20px] bg-muted p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h2 className="text-sm font-semibold tracking-[-0.01em]">{title}</h2>
          {hint && (
            <div className="max-w-3xl text-xs leading-5 text-muted-foreground">{hint}</div>
          )}
        </div>
        {aside && <div className="flex shrink-0 flex-wrap items-center gap-2">{aside}</div>}
      </div>
      <div className="mt-4">{children}</div>
    </motion.section>
  );
}

/**
 * 二次确认弹窗（受控）。危险操作（改密码、删除 API Key）必须经过它；
 * 组件本身只负责「问一句、等一个答复」，业务动作由 onConfirm 完成。
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmText = '确认',
  destructive = false,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  confirmText?: string;
  destructive?: boolean;
  onConfirm: () => void | Promise<void>;
}) {
  const [busy, setBusy] = React.useState(false);

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!busy) onOpenChange(next);
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description && <AlertDialogDescription>{description}</AlertDialogDescription>}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
          <AlertDialogAction
            disabled={busy}
            className={destructive ? 'bg-destructive text-white hover:bg-destructive/90' : ''}
            onClick={async (e) => {
              e.preventDefault();
              setBusy(true);
              try {
                await onConfirm();
                onOpenChange(false);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? '处理中…' : confirmText}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** 生成随机 API Key（与旧看板 randomKeyValue 同语义：18 字节 → 36 位 hex）。 */
export function randomKeyValue(): string {
  try {
    if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
      const bytes = new Uint8Array(18);
      crypto.getRandomValues(bytes);
      return Array.from(bytes)
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('');
    }
  } catch {
    /* 继续走时间戳兜底 */
  }
  return `wb-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * 复制到剪贴板。明文 http（局域网地址）不是安全上下文，navigator.clipboard
 * 不可用，因此保留 textarea + execCommand 兜底（与旧看板 writeClipboard 一致）。
 */
export async function writeClipboard(text: string): Promise<void> {
  if (typeof navigator !== 'undefined' && navigator.clipboard && window.isSecureContext) {
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
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } finally {
    document.body.removeChild(area);
  }
  if (!ok) throw new Error('浏览器拒绝了复制操作');
}

/** 下载一段文本为文件（机器身份导出用；剪贴板在部分环境不可用）。 */
export function downloadTextFile(filename: string, text: string, mime = 'application/json'): void {
  const blob = new Blob([text], {type: mime});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  window.setTimeout(() => {
    a.remove();
    URL.revokeObjectURL(url);
  }, 500);
}

export function MonoValue({children, className}: {children: ReactNode; className?: string}) {
  return (
    <span className={cn('font-mono text-xs break-all text-foreground', className)}>{children}</span>
  );
}
