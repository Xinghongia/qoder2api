'use client';

import * as React from 'react';
import {Download, Loader2} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {ApiError, http} from '@/lib/api';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';

/**
 * 账号导出（批量 + 单账号），行为基准是旧看板 dashboard.html 的
 * downloadExport / exportAccounts / exportOne：
 *
 *   · 必须走 fetch 拿原始文本，再手工生成 blob 下载 —— 临时 `<a href>`
 *     带不了 X-Panel-Token，纯口令会话会被拒绝（旧看板同款注释）；
 *   · 服务端返回先 JSON.parse 校验，不是 JSON 就报「返回内容不是 JSON」；
 *   · 文件清单个账号在时间戳前插入 uid 前 8 位，与批量同一文档格式，
 *     换实例导入不用改；
 *   · 时间戳表达式照抄旧看板（toISOString 是 UTC，且 T 也一并去掉）。
 */

/** 旧看板同款时间戳：YYYYMMDDHHmmss。 */
function exportStamp(): string {
  return new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '');
}

/**
 * 下载账号导出文档（uid 省略 = 全部账号）。成功返回 {count, name}。
 * 401/403 由 api 层统一弹面板登录；404 的后端 error.message 原样抛出。
 */
export async function exportAccountsFile(
  uid?: string,
): Promise<{count: number; name: string}> {
  const query = uid ? `?uid=${encodeURIComponent(uid)}` : '';
  let text: string;
  try {
    text = await http.getText(`/accounts/export${query}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      throw new Error(e.message === `HTTP ${e.status}` ? '账号不存在' : e.message);
    }
    throw e;
  }

  let doc: {count?: number} = {};
  try {
    doc = JSON.parse(text) as {count?: number};
  } catch {
    throw new Error('返回内容不是 JSON');
  }

  const label = uid ? `${uid.slice(0, 8)}-` : '';
  const name = `qoder-accounts-${label}${exportStamp()}.json`;
  const blob = new Blob([text], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return {count: doc.count || 0, name};
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** 批量导出按钮（挂在「添加账号」工具栏，与其它 outline 按钮同款）。 */
export function ExportAccountsButton({className}: {className?: string}) {
  const [busy, setBusy] = React.useState(false);

  const onClick = () => {
    if (busy) return;
    setBusy(true);
    void (async () => {
      try {
        const r = await exportAccountsFile();
        notify.ok(`已导出 ${r.count} 个账号 → ${r.name}`);
      } catch (e) {
        notify.err('导出失败', errText(e));
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      className={cn('h-8 rounded-full bg-background', className)}
      disabled={busy}
      title="导出全部账号为 JSON 文件（含凭证，可在其它实例重新导入）"
      onClick={onClick}
    >
      {busy ? (
        <Loader2 className="size-3.5 animate-spin" />
      ) : (
        <Download className="size-3.5" />
      )}
      {busy ? '导出中...' : '导出账号'}
    </Button>
  );
}
