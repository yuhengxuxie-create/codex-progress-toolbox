function Get-GuardianTaskLocation {
    $BackendRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $EcosystemRoot = [IO.Path]::GetFullPath((Join-Path $BackendRoot '..\..'))
    if ((Split-Path $BackendRoot -Leaf) -eq 'codex-feishu' -and (Test-Path -LiteralPath (Join-Path $EcosystemRoot '.codex-feishu-ecosystem-root') -PathType Leaf)) {
        $Root = $EcosystemRoot
        $Layout = 'ecosystem'
    } else {
        $Root = $BackendRoot
        $Layout = 'standalone'
    }
    $Manager = Join-Path $Root 'installer\guardian-task.ps1'
    if (-not (Test-Path -LiteralPath $Manager -PathType Leaf)) {
        throw '当前安装缺少通信监督管理器，请完成配套安装；不会恢复旧式登录直接启动任务。'
    }
    return @{Root=$Root;Layout=$Layout;Manager=$Manager}
}
