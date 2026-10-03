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
 * 面板访问密码。后端：POST /panel/password（{current, new}），
 * 校验通过后吊销其它浏览器会话并为当前会话补发新 token。
 */
export function PasswordSection({
  isDefault,
  onChanged,
}: {
  isDefault: boolean;
  onChanged: () => Promise<void>;
}) {
  const {refreshStatus} = useAuth();
  const [current, setCurrent] = React.useState('');
  const [next, setNext] = React.useState('');
  const [confirmOpen, setConfirmOpen] = React.useState(false);

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
