import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LH Trading",
  description: "台指期貨程式交易系統",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-TW">
      <body className="bg-[#0d0d14] text-[#e0e0f0] antialiased">{children}</body>
    </html>
  );
}
