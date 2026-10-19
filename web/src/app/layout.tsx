import type { Metadata } from "next";
import Link from "next/link";
import { IdentitySwitcher } from "@/components/identity-switcher";
import "./globals.css";

export const metadata: Metadata = {
  title: "企业知识库",
  description: "企业知识库问答平台（V0.1）",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // 内网部署不依赖 Google Fonts（build 时拉不到会直接失败），
  // 用系统字体栈替代 next/font/google 的 Geist。
  //
  // suppressHydrationWarning：浏览器扩展（如"通义"）会在 React hydration
  // 之前给 <html> 注入 className/data-theme/style，导致属性比对不一致报
  // hydration mismatch。该属性让 React 跳过本元素的属性比对（只影响这一层，
  // 不掩盖子组件的真实错配），是 Next.js 官方对这一场景的推荐处理。
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="antialiased">
        <header className="border-b">
          <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4">
            <nav className="flex items-center gap-6">
              <Link href="/" className="font-semibold">
                企业知识库
              </Link>
              {/* V0.1 三页：知识库列表（首页 /，T25）/ 文档上传 / 问答（T27 待建） */}
              <Link
                href="/"
                className="text-sm text-muted-foreground hover:text-foreground"
              >
                知识库
              </Link>
              <Link
                href="/documents"
                className="text-sm text-muted-foreground hover:text-foreground"
              >
                文档上传
              </Link>
            </nav>
            <IdentitySwitcher />
          </div>
        </header>
        <main className="mx-auto max-w-5xl px-4 py-8">{children}</main>
      </body>
    </html>
  );
}
