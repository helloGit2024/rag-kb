/**
 * API 客户端（V0.1 · T26）。
 *
 * - 自动带 `X-KB-User` 身份头（V0.1 无登录，身份由页面切换器写入
 *   localStorage，键名 `kb_user`，默认 admin）。
 * - 后端错误信封统一为 `{error: {code, message, detail?}}`，
 *   非 2xx 一律抛 `ApiError`，页面只管 catch 展示，不再各自解析信封。
 * - 全部走同源 `/api/v1/*`（next.config.ts rewrites 代理到 backend），
 *   浏览器永远不直连后端地址（不暴露、免 CORS）。
 */

export class ApiError extends Error {
  code: string;
  status: number;
  detail: unknown;

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export const IDENTITY_KEY = "kb_user";

export function currentIdentity(): string {
  if (typeof window === "undefined") return "admin";
  return window.localStorage.getItem(IDENTITY_KEY) || "admin";
}

export function setIdentity(user: string): void {
  window.localStorage.setItem(IDENTITY_KEY, user);
}

async function parseError(resp: Response): Promise<ApiError> {
  try {
    const body = await resp.json();
    const err = body?.error;
    return new ApiError(
      resp.status,
      err?.code ?? "UNKNOWN",
      err?.message ?? `请求失败（HTTP ${resp.status}）`,
      err?.detail,
    );
  } catch {
    // 响应体不是 JSON（如网关 502 页面）：保底仍给出状态码
    return new ApiError(resp.status, "UNKNOWN", `请求失败（HTTP ${resp.status}）`);
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("X-KB-User", currentIdentity());
  // FormData 时浏览器自动设置 multipart boundary，不要手工塞 Content-Type
  const resp = await fetch(path, { ...init, headers });
  if (!resp.ok) throw await parseError(resp);
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

// ── 后端契约类型（对齐 docs/openapi.yaml，字段名照抄）────────────────
export interface KnowledgeBase {
  id: number;
  name: string;
  description: string | null;
  default_scope: string;
  doc_count: number;
  created_at: string;
}

export interface KbListOut {
  items: KnowledgeBase[];
  total: number;
  // 契约里列表信封含 page / page_size（docs/openapi.yaml L184—189），
  // 拿来做分页；缺了它超过一页的部分会被页面静默截断。
  page: number;
  page_size: number;
}

export interface KbCreateIn {
  name: string;
  description?: string;
  default_scope?: string;
}

/** 知识库列表（契约：GET /api/v1/knowledge-bases，page 从 1 起）。 */
export function listKnowledgeBases(page = 1, pageSize = 20) {
  return apiFetch<KbListOut>(
    `/api/v1/knowledge-bases?page=${page}&page_size=${pageSize}`,
  );
}

/** 创建知识库（契约：POST /api/v1/knowledge-bases → 201；重名 409）。 */
export function createKnowledgeBase(body: KbCreateIn) {
  return apiFetch<KnowledgeBase>("/api/v1/knowledge-bases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export interface TaskStatus {
  task_id: number;
  doc_id: number | null;
  task_type: string;
  status: "pending" | "running" | "done" | "failed" | "canceled";
  progress: number;
  retry_count: number;
  error_reason: string | null;
}

export interface DocumentOut {
  id: number;
  kb_id: number;
  title: string;
  status: string;
  scope: string;
  chunk_count: number | null;
  uploaded_by: string | null;
  updated_at: string;
}

export interface DocumentListOut {
  items: DocumentOut[];
  total: number;
}
