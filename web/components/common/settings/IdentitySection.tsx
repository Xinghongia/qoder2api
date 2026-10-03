'use client';

import * as React from 'react';
import {Download, Save, Trash2, Upload} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {Textarea} from '@/components/ui/textarea';
import {api} from '@/lib/api';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';
import {
  ConfirmDialog,
  FIELD_CLASS,
  SectionCard,
  downloadTextFile,
  errText,
  type IdentityExportResult,
  type MachineIdentityView,
  type SettingsSnapshot,
} from './shared';

/**
 * 机器身份（服务器与本机对齐）。
 *
 * 官方风控按「机器身份」下发设备定向活动（每日 100 Credits 等）：装有官方
 * 客户端的 Windows 机器能取到真身份，Linux / 服务器只能回退成派生假身份，
 * 定向活动可能被静默过滤。做法是在有客户端的机器导出身份文件，到这里粘贴 /
 * 选择文件后「保存固定身份」。身份与区域无关（国内 / 国际账号共用一份）。
 *
 * 后端：GET /identity/export 取身份；POST /settings/save {machine_identity}
 * 固定，machine_identity: null 表示清除。
 */
export function IdentitySection({
  data,
  onChanged,
}: {
  data: SettingsSnapshot;
  onChanged: () => Promise<void>;
}) {
  const ident = data.machine_identity;
  const [text, setText] = React.useState('');
  const [busy, setBusy] = React.useState('');
  const [clearOpen, setClearOpen] = React.useState(false);
  const fileRef = React.useRef<HTMLInputElement | null>(null);

  const statusText = ident.pinned
    ? '已固定（来自其他机器导出的真身份）'
    : ident.bridge_available
      ? '官方风控桥 · 真身份（本机有客户端）'
      : '派生假身份（本机无客户端，定向活动可能被过滤）';
  const statusTone = ident.pinned || ident.bridge_available
    ? 'text-emerald-600 dark:text-emerald-400'
    : 'text-amber-600 dark:text-amber-400';

  const exportIdentity = async () => {
    setBusy('export');
    try {
      const r = (await api.identityExport()) as IdentityExportResult;
      if (!r?.ok || !r.identity) {
        notify.warn('导出失败', r?.reason || '未知原因');
        return;
      }
      const json = JSON.stringify(r.identity, null, 2);
      setText(json);
      downloadTextFile('qoder-machine-identity.json', json);
      notify.ok(
        '已下载 qoder-machine-identity.json',
        '把它传到服务器，用「选择文件导入」读取后点「保存固定身份」',
      );
    } catch (e) {
      notify.err('导出失败', errText(e));
    } finally {
      setBusy('');
    }
  };

  const importFile = async (file: File | null) => {
    if (!file) return;
    try {
      const content = await file.text();
      setText(content);
      notify.ok(`已读取 ${file.name}`, '点「保存固定身份」生效');
    } catch (e) {
      notify.err('读取文件失败', errText(e));
    }
  };

  const saveIdentity = async () => {
    const raw = text.trim();
    if (!raw) {
      notify.warn('先粘贴机器身份 JSON', '在装有官方客户端的机器上点「导出本机身份」');
      return;
    }
    let obj: Record<string, unknown>;
    try {
      obj = JSON.parse(raw) as Record<string, unknown>;
    } catch (e) {
      notify.err('JSON 格式不对', errText(e));
      return;
    }
    setBusy('save');
    try {
      const r = (await api.settings.save({machine_identity: obj})) as
        | {machine_identity?: MachineIdentityView}
        | undefined;
      const view = r?.machine_identity;
      notify.ok(
        '机器身份已固定',
        view?.preview ? `token ${view.preview}，签到与活动将使用该身份` : undefined,
      );
      await onChanged();
    } catch (e) {
      notify.err('保存失败', errText(e));
    } finally {
      setBusy('');
    }
  };

  const clearIdentity = async () => {
    try {
      await api.settings.save({machine_identity: null});
      notify.ok('已清除固定的机器身份');
      await onChanged();
    } catch (e) {
      notify.err('清除失败', errText(e));
    }
  };

  return (
    <SectionCard
      title="机器身份（服务器与本机对齐）"
      hint={
        '官方风控按「机器身份」下发设备定向活动；装有官方客户端的机器能自动取到真身份，Linux / 服务器只能回退成派生假身份，定向活动可能被静默过滤。身份与区域无关：在装有客户端的机器点「导出本机身份」下载文件，把文件传到服务器，用「选择文件导入」读取后点「保存固定身份」即可完全对齐。'
      }
      delay={0.08}
    >
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
        <span>
          当前：
          <span className={cn('font-semibold', statusTone)}>{statusText}</span>
        </span>
        {ident.preview && (
          <span className="text-muted-foreground">
            token {ident.preview}
            {ident.pinned_at && ` · 固定于 ${ident.pinned_at}`}
          </span>
        )}
      </div>

      <Textarea
        rows={3}
        className={cn(FIELD_CLASS, 'font-mono text-xs')}
        placeholder='粘贴身份 JSON：{"machineToken":"...","machineType":"...","machineCode":"..."}'
        value={text}
        onChange={(e) => setText(e.target.value)}
      />

      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={() => void exportIdentity()} disabled={busy === 'export'}>
          <Download className="size-4" />
          {busy === 'export' ? '导出中…' : '导出本机身份（下载文件）'}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => fileRef.current?.click()}
          disabled={!!busy}
        >
          <Upload className="size-4" />
          选择文件导入
        </Button>
        <Button size="sm" onClick={() => void saveIdentity()} disabled={!!busy}>
          <Save className="size-4" />
          {busy === 'save' ? '保存中…' : '保存固定身份'}
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="text-destructive hover:text-destructive"
          onClick={() => setClearOpen(true)}
          disabled={!!busy || !ident.pinned}
        >
          <Trash2 className="size-4" />
          清除固定
        </Button>
      </div>

      <input
        ref={fileRef}
        type="file"
        accept=".json,application/json"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0] ?? null;
          e.target.value = '';
          void importFile(file);
        }}
      />

      <ConfirmDialog
        open={clearOpen}
        onOpenChange={setClearOpen}
        title="清除固定的机器身份？"
        description="清除后将回退到本机自动获取（无官方客户端时为派生假身份），设备定向活动可能被过滤。"
        confirmText="确认清除"
        destructive
        onConfirm={clearIdentity}
      />
    </SectionCard>
  );
}
