export type DockGroupItem = {
  title: string;
  /** 分组键。相邻两项的 `groupKey` 不同时，两组之间插一条分隔线。 */
  groupKey?: string;
  /** 分组名。留空则画一条不带标签的分隔线。 */
  groupLabel?: string;
};

export type DockGroup<T> = {
  key: string;
  label: string;
  items: T[];
};

/** 把扁平条目按**相邻**的 `groupKey` 切成若干组（纯函数，零依赖）。 */
export function splitByGroup<T extends DockGroupItem>(items: T[]): DockGroup<T>[] {
  const groups: DockGroup<T>[] = [];
  for (const item of items) {
    const key = item.groupKey ?? '';
    const last = groups[groups.length - 1];
    if (last && last.key === key) {
      last.items.push(item);
      continue;
    }
    groups.push({key, label: item.groupLabel ?? '', items: [item]});
  }
  return groups;
}
