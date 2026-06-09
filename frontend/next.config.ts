import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // 用非 localhost（如 Tailscale IP / 區網 IP）存取 dev server 時，Next.js 16 預設會
  // 擋掉跨來源的 dev 資源（字型、HMR 等），導致整頁變成沒樣式的純文字。
  // 放行各機器常用來源即可；正式 build（npm run build/start）不受此限。
  allowedDevOrigins: [
    "100.97.169.26",   // Windows 開發機 Tailscale IP
    "100.127.125.13",  // Mac 家機 Tailscale IP
    "192.168.0.125",   // 區網 IP（DHCP 會變，連不到再補當下的）
  ],
};

export default nextConfig;
