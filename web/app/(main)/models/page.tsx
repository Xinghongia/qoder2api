'use client';

import * as React from 'react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {ModelCatalogTable} from '@/components/common/gateway/ModelCatalogTable';
import {useRealm} from '@/lib/realm-context';

/**
 * `/models` —— 模型库（从原「网关与运维」页拆出）。
 *
 * 官方模型清单 + 峰谷价 / 上下文窗口 / 思考档位，并可直接改这两项的
 * 「默认值」（存 settings.json，客户端请求带了值仍以客户端为准）。
 * 区域由右上角 RealmToggle 决定（同一个模型 key 在两区可能配置不同）。
 */
export default function ModelsPage() {
  const {view} = useRealm();
  const realmLabel = view === 'intl' ? '国际版' : '国内版';

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="模型库"
        description={`${realmLabel}官方模型清单；「上下文窗口 / 思考档位」可改该模型的默认值`}
      />
      <ModelCatalogTable realm={view} />
    </div>
  );
}
