Add-Type -AssemblyName PresentationFramework
[System.Windows.MessageBox]::Show(
    "这个窗口来自一个完全独立的 TreasureChest 插件。",
    "TreasureChest 插件示例",
    "OK",
    "Information"
) | Out-Null
