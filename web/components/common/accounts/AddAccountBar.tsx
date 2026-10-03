'use client';

import * as React from 'react';
import {FileJson, KeyRound, ScanLine, UserPlus} from 'lucide-react';

import {Button} from '@/components/ui/button';

import {DesktopScanDialog} from './DesktopScanDialog';
import {ImportAccountsDialog} from './ImportAccountsDialog';
import {OAuthDeviceDialog} from './OAuthDeviceDialog';
import {PatImportDialog} from './PatImportDialog';

/**
 * 「添加账号」工具栏（旧看板四个入口：OAuth 设备授权 / 本机凭证扫描 /
 * PAT 导入 / JSON 导入）。
 *
 * 四个入口成功后都只调用 onChanged() 原地刷新账号表并 notify 提示，
 * 不切换当前区域视图——旧看板曾因批量动作跳到另一区被投诉，这里保持
 * 「原地生效」。
 */

type Which = 'oauth' | 'desktop' | 'pat' | 'json' | null;

export function AddAccountBar({
  onChanged,
}: {
  /** 原地刷新账号表（页面 load()），不得改变当前区域 / 视图。 */
  onChanged: () => Promise<void> | void;
}) {
  const [which, setWhich] = React.useState<Which>(null);

  const handleOpenChange =
    (target: Exclude<Which, null>) => (open: boolean) => {
      if (!open) setWhich((cur) => (cur === target ? null : cur));
    };

  return (
    <>
      <section className="flex flex-wrap items-center gap-2 rounded-[20px] bg-muted px-4 py-3">
        <span className="mr-1 text-xs font-medium text-muted-foreground">添加账号</span>

        <Button
          type="button"
          size="sm"
          className="h-8 rounded-full"
          title="在浏览器完成 Qoder 设备授权，自动检测入池"
          onClick={() => setWhich('oauth')}
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
          onClick={() => setWhich('desktop')}
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
          onClick={() => setWhich('pat')}
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
          onClick={() => setWhich('json')}
        >
          <FileJson className="size-3.5" />
          导入 JSON
        </Button>

        <span className="ml-auto hidden text-[11px] text-muted-foreground sm:inline">
          导入后原地刷新当前列表，不会切换区域
        </span>
      </section>

      <OAuthDeviceDialog
        open={which === 'oauth'}
        onOpenChange={handleOpenChange('oauth')}
        onChanged={onChanged}
      />
      <DesktopScanDialog
        open={which === 'desktop'}
        onOpenChange={handleOpenChange('desktop')}
        onChanged={onChanged}
      />
      <PatImportDialog
        open={which === 'pat'}
        onOpenChange={handleOpenChange('pat')}
        onChanged={onChanged}
      />
      <ImportAccountsDialog
        open={which === 'json'}
        onOpenChange={handleOpenChange('json')}
        onChanged={onChanged}
      />
    </>
  );
}
