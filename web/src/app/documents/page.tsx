"use client";

/**
 * 文档上传页（V0.1 · T26）。
 *
 * 链路：选 KB → 选文件（前端预校验类型/大小，省一次必然失败的请求）
 *      → POST /documents（202 返回 task_id）→ 轮询 GET /tasks/{task_id}
 *      → done 刷新文档列表 / failed 展示人话 error_reason。
 *
 * 契约要点（docs/openapi.yaml）：
 * - 上传 202 → {task_id}；409 同名文件已存在；413 >20MB；415 类型不支持。
 *   （413/415 前端预校验先行，后端仍是权威——预校验只为体验。）
 * - 任务失败 error_reason 是"给人看的话"（T19），直接展示，不再加工。
 * - 文档六态：pending/parsing/embedding…/done/failed——徽章色映射见 STATUS_META。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useIdentityChange } from "@/components/identity-switcher";
import {
  ApiError,
  apiFetch,
  type DocumentListOut,
  type KbListOut,
  type TaskStatus,
} from "@/lib/api";

const MAX_SIZE = 20 * 1024 * 1024; // 20MB，与后端 constraints 一致
const ACCEPT = [".txt", ".md"];
const POLL_MS = 2000;

// 文档六态的展示文案与徽章色（done 之外都算"处理中"色系）
const STATUS_META: Record<string, { label: string; variant: "secondary" | "default" | "destructive" | "outline" }> = {
  pending: { label: "排队中", variant: "secondary" },
  parsing: { label: "解析中", variant: "default" },
  chunking: { label: "分块中", variant: "default" },
  embedding: { label: "向量化中", variant: "default" },
  done: { label: "完成", variant: "outline" },
  failed: { label: "失败", variant: "destructive" },
};

interface UploadState {
  phase: "uploading" | "polling" | "done" | "failed" | "error";
  taskId?: number;
  message?: string;
}

export default function DocumentsPage() {
  const [kbs, setKbs] = useState<KbListOut | null>(null);
  const [kbId, setKbId] = useState<string>("");
  const [docs, setDocs] = useState<DocumentListOut | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<UploadState | null>(null);
  const [pageError, setPageError] = useState<string>("");
  const pollTimer = useRef<number | null>(null);

  const loadKbs = useCallback(async () => {
    try {
      const data = await apiFetch<KbListOut>("/api/v1/knowledge-bases");
      setKbs(data);
      setPageError("");
    } catch (e) {
      setPageError(e instanceof ApiError ? e.message : "知识库列表加载失败");
    }
  }, []);

  const loadDocs = useCallback(async (id: string) => {
    if (!id) return;
    try {
      const data = await apiFetch<DocumentListOut>(
        `/api/v1/documents?kb_id=${id}`,
      );
      setDocs(data);
    } catch {
      // 列表拉取失败不阻塞上传主流程；404（KB 不存在）等由上传动作暴露
      setDocs({ items: [], total: 0 });
    }
  }, []);

  useEffect(() => {
    void loadKbs();
  }, [loadKbs, ]);

  useEffect(() => {
    void loadDocs(kbId);
  }, [kbId, loadDocs]);

  useIdentityChange(() => {
    void loadKbs();
    void loadDocs(kbId);
  });

  // 卸载时清掉轮询定时器，避免离开页面后仍在打接口
  useEffect(() => {
    return () => {
      if (pollTimer.current) window.clearInterval(pollTimer.current);
    };
  }, []);

  function pickFile(f: File | null) {
    setFile(f);
    setPageError("");
    if (!f) return;
    const ext = "." + f.name.split(".").pop()?.toLowerCase();
    if (!ACCEPT.includes(ext)) {
      setPageError(`仅支持 ${ACCEPT.join(" / ")} 文件（当前：${ext}）`);
      setFile(null);
      return;
    }
    if (f.size > MAX_SIZE) {
      setPageError(
        `文件超过 20MB 上限（当前 ${(f.size / 1024 / 1024).toFixed(1)}MB）`,
      );
      setFile(null);
    }
  }

  function startPolling(taskId: number) {
    if (pollTimer.current) window.clearInterval(pollTimer.current);
    pollTimer.current = window.setInterval(async () => {
      try {
        const t = await apiFetch<TaskStatus>(`/api/v1/tasks/${taskId}`);
        if (t.status === "done") {
          window.clearInterval(pollTimer.current!);
          pollTimer.current = null;
          setUpload({ phase: "done", taskId });
          void loadDocs(kbId); // 文档列表立刻出现新行
        } else if (t.status === "failed") {
          window.clearInterval(pollTimer.current!);
          pollTimer.current = null;
          setUpload({
            phase: "failed",
            taskId,
            message: t.error_reason ?? "处理失败（原因未知）",
          });
          void loadDocs(kbId);
        }
      } catch {
        // 单次轮询失败不打断轮询（后端瞬断）；连续失败由用户重试上传兜底
      }
    }, POLL_MS);
  }

  async function onUpload() {
    setPageError("");
    if (!kbId) {
      setPageError("请先选择知识库");
      return;
    }
    if (!file) {
      setPageError("请先选择文件");
      return;
    }
    setUpload({ phase: "uploading" });
    const form = new FormData();
    form.append("file", file);
    form.append("kb_id", kbId);
    try {
      const resp = await apiFetch<{ task_id: number }>("/api/v1/documents", {
        method: "POST",
        body: form,
      });
      setUpload({ phase: "polling", taskId: resp.task_id });
      startPolling(resp.task_id);
    } catch (e) {
      setUpload(null);
      if (e instanceof ApiError) {
        // 409 重名 / 415 类型 / 413 超限：信封 message 已是人话，直接展示
        setPageError(e.message);
      } else {
        setPageError("上传失败（网络异常）");
      }
    }
  }

  function resetUpload() {
    if (pollTimer.current) window.clearInterval(pollTimer.current);
    pollTimer.current = null;
    setUpload(null);
    setFile(null);
  }

  return (
    <div className="grid gap-6">
      <h1 className="text-xl font-semibold">文档上传</h1>

      {pageError && (
        <Alert variant="destructive">
          <AlertDescription>{pageError}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>上传文档</CardTitle>
          <CardDescription>
            支持 .txt / .md，≤ 20MB；上传后自动解析＋分块＋向量化
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div className="grid gap-2">
            <span className="text-sm font-medium">知识库</span>
            <Select value={kbId} onValueChange={(v) => setKbId(v ?? "")}>
              <SelectTrigger className="w-72">
                <SelectValue
                  placeholder={kbs?.total ? "选择知识库" : "（暂无知识库）"}
                />
              </SelectTrigger>
              <SelectContent>
                {kbs?.items.map((kb) => (
                  <SelectItem key={kb.id} value={String(kb.id)}>
                    {kb.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="grid gap-2">
            <span className="text-sm font-medium">文件</span>
            <Input
              type="file"
              accept={ACCEPT.join(",")}
              onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
              className="w-72"
            />
          </div>

          {upload && (
            <div className="grid gap-2 rounded-md border p-3">
              {upload.phase === "uploading" && (
                <span className="text-sm">上传中…</span>
              )}
              {upload.phase === "polling" && (
                <>
                  <span className="text-sm">
                    已受理（task_id={upload.taskId}），处理中…
                  </span>
                  <Progress value={60} />
                </>
              )}
              {upload.phase === "done" && (
                <span className="text-sm text-green-600">
                  处理完成，文档已可被问答检索。
                </span>
              )}
              {upload.phase === "failed" && (
                <Alert variant="destructive">
                  <AlertTitle>处理失败</AlertTitle>
                  <AlertDescription>{upload.message}</AlertDescription>
                </Alert>
              )}
              <div>
                <Button size="sm" variant="outline" onClick={resetUpload}>
                  再传一个
                </Button>
              </div>
            </div>
          )}

          <div>
            <Button onClick={onUpload} disabled={upload?.phase === "uploading" || upload?.phase === "polling"}>
              上传
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>该知识库下的文档</CardTitle>
          <CardDescription>
            {kbId ? `共 ${docs?.total ?? 0} 篇` : "选择知识库后显示"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {kbId && docs && docs.items.length > 0 && (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  <th className="py-2">标题</th>
                  <th className="py-2">状态</th>
                  <th className="py-2">分块</th>
                  <th className="py-2">可见范围</th>
                </tr>
              </thead>
              <tbody>
                {docs.items.map((d) => {
                  const meta = STATUS_META[d.status] ?? {
                    label: d.status,
                    variant: "secondary" as const,
                  };
                  return (
                    <tr key={d.id} className="border-b">
                      <td className="py-2 pr-4">{d.title}</td>
                      <td className="py-2 pr-4">
                        <Badge variant={meta.variant}>{meta.label}</Badge>
                      </td>
                      <td className="py-2 pr-4">{d.chunk_count ?? "—"}</td>
                      <td className="py-2">{d.scope}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {kbId && docs?.items.length === 0 && (
            <p className="text-sm text-muted-foreground">还没有文档，先传一篇。</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
