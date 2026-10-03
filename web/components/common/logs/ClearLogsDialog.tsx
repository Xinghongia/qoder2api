'use client';

import * as React from 'react';
import {Trash2} from 'lucide-react';

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';
import {Button} from '@/components/ui/button';

/** 清空日志：危险操作，先弹确认框（旧看板用 confirm，这里换成同风格弹窗）。 */
export function ClearLogsDialog({
  count,
  onConfirm,
}: {
  count: number;
  onConfirm: () => Promise<void>;
}) {
  const [open, setOpen] = React.useState(false);
  const [busy, setBusy] = React.useState(false);

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>
        <Button size="sm" variant="destructive" title="清空所有日志">
          <Trash2 className="size-4" />
          清空
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>清空网关日志？</AlertDialogTitle>
          <AlertDialogDescription>
            将清空内存中的全部日志缓冲（当前约 {count} 条），此操作不可撤销。
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
          <AlertDialogAction
            disabled={busy}
            className="bg-destructive text-white hover:bg-destructive/90"
            onClick={async (e) => {
              e.preventDefault();
              setBusy(true);
              try {
                await onConfirm();
                setOpen(false);
              } catch {
                // 失败已由调用方 notify.err 提示，这里保持弹窗打开让用户看到状态
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? '清空中…' : '确认清空'}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
