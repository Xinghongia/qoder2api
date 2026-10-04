'use client';

import * as React from 'react';
import type {ReactNode} from 'react';
import {Copy, Eye, EyeOff, KeyRound, Pencil, Plus, Trash2} from 'lucide-react';

import {EmptyState} from '@/components/common/layout/EmptyState';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Switch} from '@/components/ui/switch';
import {api} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';
import {
  ConfirmDialog,
  FIELD_CLASS,
  FieldLabel,
  SectionCard,
  errText,
  randomKeyValue,
  writeClipboard,
  type ApiKeyEntry,
  // 类型仍引用设置页的共享定义：GET /settings 的返回结构由 settings/shared.tsx
  // 统一维护，密钥页只是它的另一个消费方，不另起一份避免两处漂移。
  type SettingsSnapshot,
} from '@/components/common/settings/shared';

/**
 * API Key 与出口绑定。
 *
 * 后端只存列表、只画掩码：POST /settings/save {api_keys: [...]} 是整表替换，
 * 空 key 表示保留原值（见 _handle_settings_save），删除 = 从数组里省略；
 * 明文回显走 GET /settings/reveal?id=（复制 / 显示按钮）。
 */

interface KeyDraft {
  /** 仅前端使用的稳定 key（服务端 id 保存后才存在） */
  uid: string;
  id: string;
  name: string;
  /** 编辑中的明文；空串表示保留服务器原值 */
  key: string;
  masked: string;
  realm: string;
  enabled: boolean;
  created_at: string;
  editing: boolean;
}

let uidSeq = 0;
function nextUid(): string {
  uidSeq += 1;
  return `key-${uidSeq}`;
}

function toDraft(entry: ApiKeyEntry): KeyDraft {
  return {
    uid: nextUid(),
    id: entry.id || '',
    name: entry.name || '',
    key: '',
    masked: entry.masked || '',
    realm: entry.realm === 'intl' || entry.realm === 'cn' ? entry.realm : '',
    enabled: entry.enabled !== false,
    created_at: entry.created_at || '',
    editing: false,
  };
}

function buildPayload(rows: KeyDraft[]) {
  return rows.map((row) => ({
    id: row.id,
    name: row.name.trim(),
    realm: row.realm,
    enabled: row.enabled,
    // 空值 = 保留原值（后端逐行处理）
    key: row.key.trim(),
  }));
}

function KeyBadge({tone, children}: {tone: 'on' | 'off' | 'intl' | 'cn' | 'follow'; children: ReactNode}) {
  const tones: Record<string, string> = {
    on: 'bg-emerald-500/12 text-emerald-700 dark:text-emerald-400',
    off: 'bg-muted-foreground/15 text-muted-foreground',
    intl: 'bg-blue-500/12 text-blue-700 dark:text-blue-400',
    cn: 'bg-violet-500/12 text-violet-700 dark:text-violet-400',
    follow: 'bg-muted-foreground/15 text-muted-foreground',
  };
  return (
    <Badge variant="secondary" className={cn('border-transparent', tones[tone])}>
      {children}
    </Badge>
  );
}

export function ApiKeysSection({
  data,
  onChanged,
}: {
  data: SettingsSnapshot;
  onChanged: () => Promise<void>;
}) {
  const {mode, preferred} = useRealm();
  const [rows, setRows] = React.useState<KeyDraft[]>(() => data.api_keys.map(toDraft));
  const [revealed, setRevealed] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const [deleteTarget, setDeleteTarget] = React.useState<KeyDraft | null>(null);

  // 服务器数据刷新时重建列表；正在编辑的行保留草稿，避免输入被清掉
  React.useEffect(() => {
    setRows((prev) => (prev.some((row) => row.editing) ? prev : data.api_keys.map(toDraft)));
    setRevealed({});
  }, [data.api_keys]);

  const followLabel = mode === 'both'
    ? `跟随网关出口（双区 · 优先${preferred === 'intl' ? '国际版' : '国内版'}，失效自动切换）`
    : `跟随网关出口（仅${mode === 'intl' ? '国际版' : '国内版'}）`;
  const followBadge = mode === 'both'
    ? `跟随网关出口 · 双区优先${preferred === 'intl' ? '国际版' : '国内版'}`
    : `跟随网关出口 · 仅${mode === 'intl' ? '国际版' : '国内版'}`;

  const enabledCount = data.api_keys.filter((entry) => entry.enabled).length;

  const updateRow = (uid: string, patch: Partial<KeyDraft>) => {
    setRows((prev) => prev.map((row) => (row.uid === uid ? {...row, ...patch} : row)));
  };

  const persist = async (next: KeyDraft[], message: string) => {
    setBusy(true);
    try {
      await api.settings.save({api_keys: buildPayload(next)});
      setRows(next);
      notify.ok(message);
      await onChanged();
    } catch (e) {
      notify.err('保存失败', errText(e));
      await onChanged();
    } finally {
      setBusy(false);
    }
  };

  const addRow = () => {
    if (rows.some((row) => row.editing)) {
      notify.warn('先把正在编辑的 Key 保存或取消');
      return;
    }
    setRows((prev) => [
      ...prev,
      {
        uid: nextUid(),
        id: '',
        name: '',
        key: randomKeyValue(),
        masked: '',
        realm: '',
        enabled: true,
        created_at: '',
        editing: true,
      },
    ]);
  };

  const editRow = (uid: string) => {
    if (rows.some((row) => row.editing && row.uid !== uid)) {
      notify.warn('先把正在编辑的 Key 保存或取消');
      return;
    }
    updateRow(uid, {editing: true, key: ''});
  };

  const cancelEdit = (uid: string) => {
    setRows((prev) => {
      const row = prev.find((item) => item.uid === uid);
      if (!row) return prev;
      // 从未保存过的草稿行：直接丢弃
      if (!row.id) return prev.filter((item) => item.uid !== uid);
      const source = data.api_keys.find((item) => item.id === row.id);
      return prev.map((item) =>
        item.uid === uid ? (source ? toDraft(source) : {...item, editing: false, key: ''}) : item,
      );
    });
  };

  const saveRow = async (uid: string) => {
    const row = rows.find((item) => item.uid === uid);
    if (!row) return;
    if (!row.key.trim() && !row.masked) {
      notify.warn('API Key 不能为空', '新行需要填写或点「随机生成」');
      return;
    }
    const next = rows.map((item) => (item.uid === uid ? {...item, editing: false} : item));
    await persist(next, `API Key「${row.name.trim() || '未命名'}」已保存`);
  };

  const toggleEnabled = async (uid: string, enabled: boolean) => {
    if (rows.some((row) => row.editing)) {
      notify.warn('先把正在编辑的 Key 保存或取消');
      return;
    }
    const row = rows.find((item) => item.uid === uid);
    const next = rows.map((item) => (item.uid === uid ? {...item, enabled} : item));
    await persist(next, `Key「${row?.name || '未命名'}」已${enabled ? '启用' : '禁用'}`);
  };

  const confirmRemove = async () => {
    const target = deleteTarget;
    if (!target) return;
    setDeleteTarget(null);
    const next = rows.filter((item) => item.uid !== target.uid);
    if (!target.id) {
      // 未保存的草稿，不打扰服务器
      setRows(next);
      return;
    }
    await persist(next, `已删除 API Key「${target.name || '未命名'}」`);
  };

  const toggleReveal = async (row: KeyDraft) => {
    if (!row.id || busy) return;
    if (revealed[row.id]) {
      setRevealed((prev) => {
        const next = {...prev};
        delete next[row.id];
        return next;
      });
      return;
    }
    setBusy(true);
    try {
      const r = (await api.settings.reveal(row.id)) as {key?: string} | undefined;
      setRevealed((prev) => ({...prev, [row.id]: r?.key || ''}));
    } catch (e) {
      notify.err('读取明文失败', errText(e));
    } finally {
      setBusy(false);
    }
  };

  const copyRow = async (row: KeyDraft) => {
    try {
      let value = row.key.trim() || revealed[row.id] || '';
      if (!value && row.id) {
        const r = (await api.settings.reveal(row.id)) as {key?: string} | undefined;
        value = (r?.key || '').trim();
      }
      if (!value) {
        notify.warn('这一行还没有 Key', '点「编辑」填写或随机生成');
        return;
      }
      await writeClipboard(value);
      notify.ok('已复制到剪贴板');
    } catch (e) {
      notify.err('复制失败', errText(e));
    }
  };

  return (
    <SectionCard
      title="API Key 与出口绑定"
      hint={
        '每个 Key 可绑定专属出口。未绑定的 Key 跟随网关出口模式（双区 = 优先区失效自动切换）；绑定后的 Key 固定走该出口、不参与失效切换，适合给不同客户端分派固定区域。'
      }
      aside={
        <>
          <KeyBadge tone={enabledCount > 0 ? 'on' : 'off'}>
            {enabledCount > 0
              ? `${enabledCount} 个生效`
              : data.auth_required
                ? '未启用面板 Key'
                : '当前不校验'}
          </KeyBadge>
          <Button size="sm" onClick={addRow} disabled={busy}>
            <Plus className="size-4" />
            添加 API Key
          </Button>
        </>
      }
      delay={0.12}
    >
      {data.api_key_set && !data.api_key_set_by_panel && data.api_keys.length === 0 && (
        <div className="mb-3 rounded-xl bg-background/70 px-3 py-2 text-[11px] leading-5 text-muted-foreground dark:bg-white/[0.04]">
          当前仍在校验启动参数 Key（{data.api_key_masked}）；添加面板 Key 后，只有面板 Key 生效。
        </div>
      )}

      {rows.length === 0 ? (
        <EmptyState
          icon={KeyRound}
          title="还没有 API 密钥"
          description="点击下方「新建密钥」创建第一把；未绑定出口的密钥会跟随网关出口。"
          className="flex flex-col items-center justify-center py-10 text-center"
        >
          <Button size="sm" onClick={addRow} disabled={busy}>
            <Plus className="size-4" />
            新建密钥
          </Button>
        </EmptyState>
      ) : (
        <div className="space-y-2.5">
          {rows.map((row) => {
            const plain = row.id ? revealed[row.id] : '';
            const displayValue = plain || row.masked || '未设置';
            return (
              <div
                key={row.uid}
                className={cn(
                  'rounded-2xl bg-background/70 px-3.5 py-3 dark:bg-white/[0.04]',
                  !row.enabled && !row.editing && 'opacity-60',
                )}
              >
                {row.editing ? (
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <FieldLabel>备注名称</FieldLabel>
                      <Input
                        className={FIELD_CLASS}
                        autoFocus={!row.id}
                        placeholder="如: Cursor / 本地测试"
                        value={row.name}
                        onChange={(e) => updateRow(row.uid, {name: e.target.value})}
                      />
                    </div>
                    <div>
                      <FieldLabel>出口绑定</FieldLabel>
                      <Select
                        value={row.realm || 'follow'}
                        onValueChange={(value) =>
                          updateRow(row.uid, {realm: value === 'follow' ? '' : value})
                        }
                      >
                        <SelectTrigger className={cn(FIELD_CLASS, 'w-full')}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="follow">{followLabel}</SelectItem>
                          <SelectItem value="intl">固定国际版出口（不自动切换）</SelectItem>
                          <SelectItem value="cn">固定国内版出口（不自动切换）</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="sm:col-span-2">
                      <FieldLabel>
                        API Key 内容
                        {row.masked && (
                          <span className="ml-2 font-normal">（已保存 {row.masked}，留空表示不改）</span>
                        )}
                      </FieldLabel>
                      <div className="flex gap-2">
                        <Input
                          className={cn(FIELD_CLASS, 'font-mono')}
                          placeholder={
                            row.masked ? '留空表示保持原 Key 不变' : '输入自定义密钥，或点击右侧生成'
                          }
                          value={row.key}
                          onChange={(e) => updateRow(row.uid, {key: e.target.value})}
                        />
                        <Button
                          variant="outline"
                          size="sm"
                          className="shrink-0"
                          onClick={() => updateRow(row.uid, {key: randomKeyValue()})}
                        >
                          随机生成
                        </Button>
                      </div>
                    </div>
                    <div className="flex justify-end gap-2 sm:col-span-2">
                      <Button variant="outline" size="sm" onClick={() => cancelEdit(row.uid)} disabled={busy}>
                        取消
                      </Button>
                      <Button size="sm" onClick={() => void saveRow(row.uid)} disabled={busy}>
                        保存
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="min-w-0 space-y-1.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold">{row.name || '未命名'}</span>
                        <KeyBadge tone={row.enabled ? 'on' : 'off'}>
                          {row.enabled ? '已启用' : '已禁用'}
                        </KeyBadge>
                        <KeyBadge
                          tone={row.realm === 'intl' ? 'intl' : row.realm === 'cn' ? 'cn' : 'follow'}
                        >
                          {row.realm === 'intl'
                            ? '固定国际版出口 · 不自动切换'
                            : row.realm === 'cn'
                              ? '固定国内版出口 · 不自动切换'
                              : followBadge}
                        </KeyBadge>
                      </div>
                      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                        <span>
                          Key: <span className="font-mono text-foreground">{displayValue}</span>
                        </span>
                        {row.created_at && <span>创建时间 {row.created_at}</span>}
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-1">
                      {row.id && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => void toggleReveal(row)}
                          disabled={busy}
                        >
                          {revealed[row.id] ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                          {revealed[row.id] ? '隐藏' : '显示'}
                        </Button>
                      )}
                      <Button variant="ghost" size="sm" onClick={() => void copyRow(row)} disabled={busy}>
                        <Copy className="size-4" />
                        复制
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => editRow(row.uid)} disabled={busy}>
                        <Pencil className="size-4" />
                        编辑
                      </Button>
                      <Switch
                        checked={row.enabled}
                        disabled={busy}
                        aria-label={`${row.enabled ? '禁用' : '启用'} ${row.name || '未命名'}`}
                        onCheckedChange={(checked) => void toggleEnabled(row.uid, checked)}
                      />
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-destructive hover:text-destructive"
                        onClick={() => setDeleteTarget(row)}
                        disabled={busy}
                      >
                        <Trash2 className="size-4" />
                        删除
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        title={`删除 API Key「${deleteTarget?.name || '未命名'}」？`}
        description="删除后使用该 Key 的客户端会立即失去访问权限，操作不可撤销。"
        confirmText="确认删除"
        destructive
        onConfirm={confirmRemove}
      />
    </SectionCard>
  );
}
