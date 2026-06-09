import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // 用非 localhost（如 Tailscale IP）開發機存取 dev server 時，Next.js 16 預設會
  // 擋掉跨來源的 dev 資源（字型、HMR 等），導致整頁變成沒樣式的純文字。
  // 放行本機常用來源即可；正式 build（npm run build/start）不受此限。
  allowedDevOrigins: [
    "100.127.125.13", // 本機 Tailscale IP（手機/其他機器用這個連）
    "192.168.0.125", // 區網 IP（會隨 DHCP 變，之後連不到再補當下的）
  ],
};

export default nextConfig;
