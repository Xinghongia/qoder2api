'use client';

import * as React from 'react';
import {TriangleAlert} from 'lucide-react';

import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {useAuth} from '@/lib/auth-context';
import {api, setPanelToken} from '@/lib/api';
import {notify} from '@/lib/toast';
import {ConfirmDialog, FIELD_CLASS, FieldLabel, SectionCard, errText} from './shared';

/**
 * 面板访问密码与登录账号。后端：POST /panel/password（{current, new}），
 * 校验通过后吊销其它浏览器会话并为当前会话补发新 token；
 * 登录账号单独走 POST /settings/save {panel_username}，改账号不需要密码。
 */
export function PasswordSection({
  isDefault,
  username,
  onChanged,
}: {
  isDefault: boolean;
  /** 当前登录账号（GET /settings 的 panel_username，默认 admin） */
  username: string;
  onChanged: () => Promise<void>;
}) {
  const {refreshStatus} = useAuth();
  const [account, setAccount] = React.useState(username);
  const [savingAccount, setSavingAccount] = React.useState(false);
  const [current, setCurrent] = React.useState('');
  const [next, setNext] = React.useState('');
  const [confirmOpen, setConfirmOpen] = React.useState(false);

  // 服务器数据刷新后同步账号初值（密码输入框里的内容不受影响）
  React.useEffect(() => {
    setAccount(username);
  }, [username]);

  const saveAccount = async () => {
    const value = account.trim();
    if (!value) {
      notify.warn('登录账号不能为空');
      return;
    }
    if (value.length > 64) {
      notify.warn('登录账号最长 64 个字符');
      return;
    }
    setSavingAccount(true);
    try {
      await api.settings.save({panel_username: value});
      notify.ok('登录账号已更新', '下次登录请使用新账号');
      await onChanged();
    } catch (e) {
      notify.err('保存失败', errText(e));
    } finally {
      setSavingAccount(false);
    }
  };

  const requestChange = () => {
    if (!current || !next) {
      notify.warn('请填写当前密码和新密码');
      return;
    }
    if (next.length < 4) {
      notify.warn('新密码至少 4 位');
      return;
    }
    setConfirmOpen(true);
  };

  const doChange = async () => {
    try {
      const r = await api.panel.password(current, next);
      if (r?.token) setPanelToken(r.token);
      setCurrent('');
      setNext('');
      notify.ok('面板密码已更新', '其它窗口的面板会话已失效，本窗口已自动续期');
      await refreshStatus();
      await onChanged();
    } catch (e) {
      notify.err('修改失败', errText(e));
    }
  };

  return (
    <SectionCard
      title="面板访问密码"
      hint={
        '用于登录本看板；所有管理接口都需要面板会话。默认密码为 admin，忘记密码时可清空 accounts/settings.json 里的 panel_password_* 字段，恢复默认 admin。'
      }
      aside={
        isDefault ? (
          <Badge variant="secondary" className="border-transparent bg-amber-500/12 text-amber-700 dark:text-amber-400">
            当前为默认密码
          </Badge>
        ) : (
          <Badge variant="secondary" className="border-transparent bg-emerald-500/12 text-emerald-700 dark:text-emerald-400">
            已自定义
          </Badge>
        )
      }
      delay={0}
    >
      {isDefault && (
        <div className="mb-4 flex items-start gap-2 rounded-xl bg-amber-500/10 px-3 py-2 text-xs leading-5 text-amber-700 dark:text-amber-400">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
          <span>
            当前仍在使用默认密码 <span className="font-mono font-semibold">admin</span>，任何能访问本端口的人都可以打开面板，建议立即修改。
          </span>
        </div>
      )}

      <div className="mb-3">
        <FieldLabel>登录账号</FieldLabel>
        <div className="flex gap-2">
          <Input
            autoComplete="username"
            className={`${FIELD_CLASS} flex-1`}
            placeholder="admin"
            value={account}
            onChange={(e) => setAccount(e.target.value)}
          />
          <Button
            variant="outline"
            size="sm"
            className="shrink-0"
            disabled={savingAccount}
            onClick={() => void saveAccount()}
          >
            {savingAccount ? '保存中…' : '保存账号'}
          </Button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <FieldLabel>当前密码</FieldLabel>
          <Input
            type="password"
            autoComplete="current-password"
            className={FIELD_CLASS}
            placeholder="当前密码"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </div>
        <div>
          <FieldLabel>新密码（至少 4 位）</FieldLabel>
          <Input
            type="password"
            autoComplete="new-password"
            className={FIELD_CLASS}
            placeholder="至少 4 位"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        </div>
      </div>

      <div className="mt-4">
        <Button size="sm" onClick={requestChange}>
          修改密码
        </Button>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="确认修改面板密码？"
        description="修改后其它浏览器上的面板会话会全部失效，需要用新密码重新登录；本窗口会自动续期。"
        confirmText="确认修改"
        onConfirm={doChange}
      />
    </SectionCard>
  );
}
