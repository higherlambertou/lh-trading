# 模擬盤監看啟動器（watchdog）— Windows PowerShell 版
# ─────────────────────────────────────────────────────────────
# 為什麼需要：shioaji SDK 原生層在永豐 session 不穩時會卡在 Solace I/O
# 又不釋放 GIL，凍結整個 Python（含 asyncio event loop）、/health 變 000。
# 純 Python 改不掉，故用外部監看：偵測凍結就自動重啟。重啟安全（啟動會自動對帳）。
#
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File .\run_sim.ps1
#
# 停止：在此視窗按 Ctrl+C（會一併關掉子進程）。

$ErrorActionPreference = "Stop"

$Port            = 8003
$HealthUrl       = "http://localhost:$Port/api/health"
$AppLog          = Join-Path $env:TEMP "lh_sim.out.log"
$ErrLog          = Join-Path $env:TEMP "lh_sim.err.log"
$CheckInterval   = 15      # 每幾秒檢查一次 health
$StartupTimeout  = 120     # 啟動最多等幾秒
$FailThreshold   = 2       # 連續幾次 health 失敗才判定凍結

function Write-Log { param([string]$Msg) Write-Host ("{0}  [watchdog-sim]  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Msg) }

function Get-HealthCode {
    try {
        $resp = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        return [int]$resp.StatusCode
    } catch { return 0 }
}

# 啟動前先清掉監聽該 port 的殘留進程，避免綁不上
function Clear-Port {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        foreach ($pid in ($conns.OwningProcess | Select-Object -Unique)) {
            try { Stop-Process -Id $pid -Force -ErrorAction Stop; Write-Log "清掉佔用 $Port 的進程 PID=$pid" } catch {}
        }
        Start-Sleep -Seconds 2
    }
}

$child = $null
try {
    while ($true) {
        Clear-Port
        Write-Log "啟動 main_sim.py…"
        $child = Start-Process -FilePath "python" -ArgumentList "main_sim.py" `
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
        Write-Log "收到結束訊號，關閉模擬盤 PID=$($child.Id)…"
        Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
    }
}
