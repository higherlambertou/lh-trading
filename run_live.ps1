# 正式盤監看啟動器（watchdog）— Windows PowerShell 版
# ─────────────────────────────────────────────────────────────
# 為什麼需要：shioaji SDK 凍結時會卡死整個服務、/health 變 000，而正式盤凍住時
# 持倉的停損停利完全失效（裸著部位放生），比模擬盤危險。故用外部監看自動重啟。
# 重啟安全：啟動流程會自動對帳既有部位、清理殘留委託。
#
# ⚠️ 這是「正式盤・真實下單・真錢」，啟動前確認 .env 的 SIMULATION=false。
#
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File .\run_live.ps1
#
# 停止：在此視窗按 Ctrl+C（會一併關掉子進程）。

$ErrorActionPreference = "Stop"

$Port            = 8002
$CheckInterval   = 15
$StartupTimeout  = 120
$FailThreshold   = 2
$AppLog          = Join-Path $env:TEMP "lh_live.out.log"
$ErrLog          = Join-Path $env:TEMP "lh_live.err.log"

# 正式盤 main.py 綁定 .env 的 BIND_HOST（多機部署用），健康檢查必須打同一個 host，
# 否則會誤判「啟動失敗」而狂殺健康進程。沒設則 fallback localhost。
$BindHost = "localhost"
if (Test-Path ".env") {
    $m = Get-Content ".env" | Where-Object { $_ -match '^\s*BIND_HOST\s*=' } | Select-Object -First 1
    if ($m) { $BindHost = ($m -replace '^\s*BIND_HOST\s*=', '').Trim().Trim('"').Trim("'") }
    if (-not $BindHost) { $BindHost = "localhost" }
}
# 健檢一律走 loopback：BIND_HOST=0.0.0.0 時不能拿來當 client URL；
# 綁特定 Tailscale IP 時，從本機自連自己的 100.x IP 會 timeout（Tailscale on Windows 已知問題），
# 會害 watchdog 誤判凍結而狂殺健康後端。後端只要綁 0.0.0.0 就一定聽得到 127.0.0.1。
$HealthHost = if ($BindHost -in @("0.0.0.0", "", "localhost")) { "127.0.0.1" } else { $BindHost }
$HealthUrl = "http://${HealthHost}:$Port/api/health"

function Write-Log { param([string]$Msg) Write-Host ("{0}  [watchdog-live]  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Msg) }

function Get-HealthCode {
    try {
        $resp = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        return [int]$resp.StatusCode
    } catch { return 0 }
}

function Clear-Port {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        foreach ($pid in ($conns.OwningProcess | Select-Object -Unique)) {
            try { Stop-Process -Id $pid -Force -ErrorAction Stop; Write-Log "清掉佔用 $Port 的進程 PID=$pid" } catch {}
        }
        Start-Sleep -Seconds 2
    }
}

Write-Log "健康檢查目標：$HealthUrl"
$child = $null
try {
    while ($true) {
        Clear-Port
        Write-Log "啟動 main.py（正式盤・真錢）…"
        $child = Start-Process -FilePath "python" -ArgumentList "main.py" `
            -PassThru -NoNewWindow -RedirectStandardOutput $AppLog -RedirectStandardError $ErrLog
        Write-Log "已啟動 PID=$($child.Id)，等待 startup（最多 ${StartupTimeout}s）…"

        # ── 等待 startup 完成 ──
        $up = $false
        $waited = 0
        while ($waited -lt $StartupTimeout) {
            if ($child.HasExited) { Write-Log "進程在 startup 期間就結束了（登入連續失敗？）"; break }
            if ((Get-HealthCode) -eq 200) { $up = $true; break }
            Start-Sleep -Seconds 5
            $waited += 5
        }

        if (-not $up) {
            Write-Log "startup 未就緒，kill 後重啟"
            if (-not $child.HasExited) { Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 5
            continue
        }

        Write-Log "startup 完成，開始健康監看（每 ${CheckInterval}s，連續 $FailThreshold 次失敗即重啟）"

        # ── 執行中健康監看 ──
        $fails = 0
        while ($true) {
            Start-Sleep -Seconds $CheckInterval
            if ($child.HasExited) { Write-Log "進程已不存在（自行結束），重啟"; break }
            $code = Get-HealthCode
            if ($code -eq 200) {
                $fails = 0
            } else {
                $fails++
                Write-Log "health=$code（連續 $fails/$FailThreshold 次失敗）"
                if ($fails -ge $FailThreshold) {
                    Write-Log "判定凍結 → kill PID=$($child.Id) 並重啟"
                    Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 3
                    break
                }
            }
        }
        Start-Sleep -Seconds 3
    }
}
finally {
    if ($child -and -not $child.HasExited) {
        Write-Log "收到結束訊號，關閉正式盤 PID=$($child.Id)…"
        Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
    }
}
