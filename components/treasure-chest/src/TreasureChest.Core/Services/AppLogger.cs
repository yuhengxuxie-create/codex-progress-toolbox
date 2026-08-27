namespace TreasureChest.Core.Services;

public sealed class AppLogger
{
    private readonly object _gate = new();
    public AppLogger(string path)
    {
        Path = System.IO.Path.GetFullPath(path);
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
    }

    public string Path { get; }

    public void Info(string message) => Write("INFO", message);
    public void Error(string message) => Write("ERROR", message);

    private void Write(string level, string message)
    {
        var safe = (message ?? string.Empty).Replace("\r", " ").Replace("\n", " ");
        lock (_gate)
            File.AppendAllText(Path, $"{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss.fff zzz} {level} {safe}{Environment.NewLine}");
    }
}
