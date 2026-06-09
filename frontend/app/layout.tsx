import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LH Trading",
  description: "台指期貨程式交易系統",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-TW">
      {/* suppressHydrationWarning：瀏覽器擴充功能（如 Monica）會在 body 注入
          monica-id 等屬性，造成 hydration 不一致警告。忽略 body 這層即可，
          不影響功能，也不會掩蓋 children 內真正的 mismatch。 */}
      <body
        className="bg-[#0d0d14] text-[#e0e0f0] antialiased"
        suppressHydrationWarning
      >
        {children}
      </body>
    </html>
  );
}
