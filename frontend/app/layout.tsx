import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LH Trading",
  description: "台指期貨程式交易系統",
};

// 說明文字的開關（components/HelpToggle.tsx）記在 localStorage；在第一次繪製前就把它讀回 <html data-help>，
// 不然打開過的人每次載入都會先看到沒有說明的畫面再跳出來。
const HELP_INIT = "try{if(localStorage.getItem('help:on')==='1')document.documentElement.dataset.help='on'}catch(e){}";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-TW" suppressHydrationWarning>
      {/* suppressHydrationWarning：瀏覽器擴充功能（如 Monica）會在 body 注入
          monica-id 等屬性，造成 hydration 不一致警告。忽略 body 這層即可，
          不影響功能，也不會掩蓋 children 內真正的 mismatch。html 那層是 data-help（上面的腳本在 hydration 前就設了）。 */}
      <body
        className="bg-[#0d0d14] text-[#e0e0f0] antialiased"
        suppressHydrationWarning
      >
        <script dangerouslySetInnerHTML={{ __html: HELP_INIT }} />
        {children}
      </body>
    </html>
  );
}
