"use client";

/**
 * 新建知识库表单（V0.1 · T25）。
 *
 * 做成**页内展开**而不是弹窗：弹窗要走 shadcn 的 dialog 组件（T26 装组件时
 * 踩过 CLI 的 TLS 证书坑），而 V0.1 只要"能新建"这一条验收，不值得再引入
 * 一层交互组件。样式与文档上传页保持一致（Card + Input + Button）。
 *
 * 校验分工：
 * - 前端只拦"明显不该发请求"的情况（空名），省一次必然失败的往返；
 * - 长度上限（name 100 / description 500）、重名（409）、非法 scope（400）
 *   一律由后端判定——前端复刻一套规则早晚会与后端漂移。
 */

import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ApiError, createKnowledgeBase } from "@/lib/api";

const MAX_NAME_LEN = 100;
const MAX_DESCRIPTION_LEN = 500;

export function KbCreateForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scope, setScope] = useState("internal");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  async function submit() {
    setError("");
    const cleanName = name.trim();
    if (!cleanName) {
      setError("知识库名称不能为空");
      return;
    }
    if (cleanName.length > MAX_NAME_LEN) {
      setError(`知识库名称不能超过 ${MAX_NAME_LEN} 个字符`);
      return;
    }
    if (description.trim().length > MAX_DESCRIPTION_LEN) {
      setError(`描述不能超过 ${MAX_DESCRIPTION_LEN} 个字符`);
      return;
    }
    setSaving(true);
    try {
      const cleanDesc = description.trim();
      const cleanScope = scope.trim();
      await createKnowledgeBase({
        name: cleanName,
        // 空 description 不传：契约里它是可选的，传空串与不传语义不同
        ...(cleanDesc ? { description: cleanDesc } : {}),
        // 空 scope 也不传：后端会用契约默认值 internal（等价，但少一次解析）
        ...(cleanScope ? { default_scope: cleanScope } : {}),
      });
      setName("");
      setDescription("");
      setScope("internal");
      onCreated();
    } catch (e) {
      // 409 重名 / 400 非法 scope：信封 message 已是人话，直接展示
      setError(e instanceof ApiError ? e.message : "创建失败（网络异常）");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>新建知识库</CardTitle>
        <CardDescription>
          默认可见范围决定「上传时不显式指定 scope 的新文档」归哪个范围
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <div className="grid gap-2">
          <span className="text-sm font-medium">名称</span>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="例如：产品部制度文档"
            className="w-72"
            aria-label="知识库名称"
          />
        </div>

        <div className="grid gap-2">
          <span className="text-sm font-medium">
            描述<span className="text-muted-foreground">（可选）</span>
          </span>
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="一句话说明这个知识库放什么"
            className="w-96"
            aria-label="知识库描述"
          />
        </div>

        <div className="grid gap-2">
          <span className="text-sm font-medium">默认可见范围</span>
          <Input
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            placeholder="internal"
            className="w-72"
            aria-label="默认可见范围"
          />
          <span className="text-xs text-muted-foreground">
            可填 internal / public / dept:研发部 / kb:3 —— 具体取值由后端校验
          </span>
        </div>

        <div>
          <Button onClick={submit} disabled={saving}>
            {saving ? "创建中…" : "创建"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
