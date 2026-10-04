'use client';

import {FileJson, KeyRound, ScanLine, UserPlus} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {useAddAccount} from '@/lib/add-account-context';

/**
 * 「添加账号」工具栏（账号页顶部）：四个入口与旧看板一致 ——
 * OAuth 设备授权 / 本机凭证扫描 / PAT 导入 / JSON 导入。
 *
 * 弹窗由 AddAccountProvider 统一挂载（底部栏的 + 按钮打开的是同一批），
 * 成功后 Provider 递增 refreshKey，账号页据此原地刷新，不切换区域。
 */
export function AddAccountBar() {
  const {open} = useAddAccount();

  return (
    <section className="flex flex-wrap items-center gap-2 rounded-[20px] bg-muted px-4 py-3">
      <span className="mr-1 text-xs font-medium text-muted-foreground">添加账号</span>

      <Button
        type="button"
        size="sm"
        className="h-8 rounded-full"
        title="在浏览器完成 Qoder 设备授权，自动检测入池"
        onClick={() => open('oauth')}
      >
        <UserPlus className="size-3.5" />
        OAuth 设备授权
      </Button>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-8 rounded-full bg-background"
        title="只读扫描本机桌面 App / CLI 已登录的凭证，逐个导入"
        onClick={() => open('desktop')}
      >
        <ScanLine className="size-3.5" />
        扫描本机凭证
      </Button>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-8 rounded-full bg-background"
        title="粘贴 pt- 开头的 Personal Access Token 导入"
        onClick={() => open('pat')}
      >
        <KeyRound className="size-3.5" />
        导入 PAT
      </Button>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-8 rounded-full bg-background"
        title="选择文件或粘贴 JSON，预检后导入"
        onClick={() => open('json')}
      >
        <FileJson className="size-3.5" />
        导入 JSON
      </Button>

      <span className="ml-auto hidden text-[11px] text-muted-foreground sm:inline">
        导入后原地刷新当前列表，不会切换区域
      </span>
    </section>
  );
}
