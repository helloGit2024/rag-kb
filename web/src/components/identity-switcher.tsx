"use client";

/**
 * 身份切换器（V0.1 · T26，三页共用）。
 *
 * V0.1 没有登录体系（宪法：❌ 不做登录/SSO），后端靠 `X-KB-User` 头
 * 识别测试账号。前端把它做成页面顶部的显式切换器而不是环境变量：
 * 验收权限场景（列表 total=0、删除 404、拒答）需要随时换身份，
 * 改环境变量要重启、体验上等于没法验收。
 *
 * 切换后广播 `kb-identity-changed` 事件，各页面监听并重新拉数据——
 * 不用刷新整页（Next 状态会丢）。
 */

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { IDENTITY_KEY, currentIdentity, setIdentity } from "@/lib/api";

export const IDENTITY_CHANGED_EVENT = "kb-identity-changed";

export function IdentitySwitcher() {
  const [value, setValue] = useState("admin");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setValue(currentIdentity());
  }, []);

  function apply() {
    const clean = value.trim() || "admin";
    setIdentity(clean);
    setValue(clean);
    setSaved(true);
    window.setTimeout(() => setSaved(false), 1500);
    window.dispatchEvent(new CustomEvent(IDENTITY_CHANGED_EVENT));
  }

  return (
    <div className="flex items-center gap-2">
      <span className="text-sm text-muted-foreground">当前身份</span>
      <Input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && apply()}
        className="h-8 w-40"
        aria-label="X-KB-User 身份"
        placeholder="admin"
      />
      <Button size="sm" variant="outline" className="h-8" onClick={apply}>
        {saved ? "已切换" : "切换"}
      </Button>
    </div>
  );
}

/** 页面用：身份变化时重新拉数据（onIdentityChange 里 setState 触发重取）。 */
export function useIdentityChange(onChange: () => void) {
  useEffect(() => {
    const handler = () => onChange();
    window.addEventListener(IDENTITY_CHANGED_EVENT, handler);
    return () => window.removeEventListener(IDENTITY_CHANGED_EVENT, handler);
  }, [onChange]);
}

// localStorage 键名也从这里导出，避免别处写错字符串
export { IDENTITY_KEY };
