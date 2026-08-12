/**
 * 企业门户导航库 —— 路由/面包屑/激活态纯函数。
 */

export interface NavItem {
  path: string;
  label: string;
  children?: NavItem[];
}

export const NAV_ITEMS: NavItem[] = [
  { path: "/", label: "首页" },
  { path: "/products", label: "产品中心" },
  { path: "/news", label: "新闻中心" },
  { path: "/about", label: "关于我们" },
  { path: "/contact", label: "联系我们" },
];

/** 判断路径是否激活（精确匹配或子路径匹配） */
export function isActivePath(path: string, current: string): boolean {
  if (current === path) return true;
  if (path === "/") return false; // 首页仅精确匹配
  return current.startsWith(path);
}

/** 面包屑：根据当前路径生成层级 ["首页", "产品中心", ...] */
export function buildBreadcrumb(path: string): { path: string; label: string }[] {
  const parts = path.split("/").filter(Boolean);
  const crumbs: { path: string; label: string }[] = [{ path: "/", label: "首页" }];
  let acc = "";
  for (const p of parts) {
    acc += `/${p}`;
    const item = NAV_ITEMS.find((n) => n.path === acc);
    if (item) {
      crumbs.push({ path: item.path, label: item.label });
    }
  }
  return crumbs;
}

/** 根据路径找当前激活的顶层导航项；找不到返回 null */
export function activeNavItem(path: string): NavItem | null {
  return NAV_ITEMS.find((n) => isActivePath(n.path, path)) ?? null;
}

/** 生成页脚导航分组 */
export function footerNavGroups(): { title: string; links: NavItem[] }[] {
  return [
    { title: "产品", links: NAV_ITEMS.filter((n) => n.path.startsWith("/products")) },
    { title: "资讯", links: NAV_ITEMS.filter((n) => n.path.startsWith("/news")) },
    { title: "公司", links: NAV_ITEMS.filter((n) => n.path === "/about" || n.path === "/contact") },
  ];
}
