using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TreasureChest.Services;

public sealed record EcosystemInstallation(
    Version Version,
    string DisplayVersion,
    bool IsManaged,
    string InstallRoot,
    string ManagementMessage)
{
    public static EcosystemInstallation Load(string installRoot)
    {
        var root = Path.GetFullPath(installRoot);
        var metadataPath = Path.Combine(root, ".ecosystem", "installation.json");
        var sentinelPath = Path.Combine(root, ".codex-feishu-ecosystem-root");
        if (File.Exists(metadataPath) && File.Exists(sentinelPath))
        {
            try
            {
                using var document = JsonDocument.Parse(File.ReadAllText(metadataPath));
                var rootElement = document.RootElement;
                if (rootElement.TryGetProperty("schema_version", out var schema) && schema.GetInt32() == 1 &&
                    rootElement.TryGetProperty("ecosystem_version", out var versionElement) &&
                    EcosystemVersion.TryParse(versionElement.GetString(), out var managedVersion))
                {
                    return new EcosystemInstallation(managedVersion, EcosystemVersion.Display(managedVersion), true, root,
                        "当前安装由生态更新器管理，可以一键原地升级。");
                }
            }
            catch (Exception error) when (error is JsonException or IOException or UnauthorizedAccessException)
            {
                return FromAssembly(root, "安装记录无法读取，已禁止自动安装：" + error.Message);
            }
        }

        return FromAssembly(root, "当前是开发版或非托管安装，可以检查新版，但不能自动覆盖本机源码。");
    }

    private static EcosystemInstallation FromAssembly(string root, string message)
    {
        var assemblyVersion = typeof(EcosystemUpdateService).Assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
        if (!EcosystemVersion.TryParse(assemblyVersion, out var version))
            version = typeof(EcosystemUpdateService).Assembly.GetName().Version ?? new Version(0, 0, 0);
        return new EcosystemInstallation(version, EcosystemVersion.Display(version), false, root, message);
    }
}

public sealed record EcosystemPackageAsset(
    string Name,
    Uri DownloadUri,
    string Sha256,
    long Size,
    string PackageKind);

public sealed record EcosystemReleaseInfo(
    Version Version,
    string DisplayVersion,
    string Name,
    string ReleaseNotes,
    Uri ReleasePage,
    DateTimeOffset? PublishedAt,
    EcosystemPackageAsset Package);

public sealed record UpdateCheckResult(
    bool NotModified,
    bool IsUpdateAvailable,
    string ETag,
    EcosystemReleaseInfo? Release);

public sealed record UpdateDownloadProgress(long BytesReceived, long? TotalBytes)
{
    public int Percentage => TotalBytes is > 0
        ? Math.Clamp((int)Math.Round(BytesReceived * 100d / TotalBytes.Value), 0, 100)
        : 0;
}

public static class EcosystemVersion
{
    public static bool TryParse(string? value, out Version version)
    {
        version = new Version(0, 0, 0);
        if (string.IsNullOrWhiteSpace(value)) return false;
        var normalized = value.Trim().TrimStart('v', 'V');
        var suffix = normalized.IndexOfAny(['-', '+']);
        if (suffix >= 0) normalized = normalized[..suffix];
        if (!Version.TryParse(normalized, out var parsed) || parsed is null) return false;
        version = new Version(parsed.Major, Math.Max(0, parsed.Minor), Math.Max(0, parsed.Build));
        return true;
    }

    public static string Display(Version version) => $"v{version.Major}.{version.Minor}.{Math.Max(0, version.Build)}";
}

public sealed class EcosystemUpdateService : IDisposable
{
    public const string RepositoryWebRoot = "https://github.com/yuhengxuxie-create/codex-progress-toolbox";
    private static readonly Uri LatestReleaseApi = new(
        "https://api.github.com/repos/yuhengxuxie-create/codex-progress-toolbox/releases/latest");
    private readonly HttpClient _httpClient;
    private readonly bool _ownsClient;

    public EcosystemUpdateService(HttpClient? httpClient = null)
    {
        _ownsClient = httpClient is null;
        _httpClient = httpClient ?? new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
        if (!_httpClient.DefaultRequestHeaders.UserAgent.Any())
            _httpClient.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("TreasureChest", "1.0"));
        _httpClient.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/vnd.github+json"));
        _httpClient.DefaultRequestHeaders.Add("X-GitHub-Api-Version", "2022-11-28");
    }

    public async Task<UpdateCheckResult> CheckAsync(
        Version currentVersion,
        string? etag = null,
        CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, LatestReleaseApi);
        if (!string.IsNullOrWhiteSpace(etag) && EntityTagHeaderValue.TryParse(etag, out var parsedEtag))
            request.Headers.IfNoneMatch.Add(parsedEtag);
        using var response = await _httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
            .ConfigureAwait(false);
        var responseEtag = response.Headers.ETag?.ToString() ?? etag ?? string.Empty;
        if (response.StatusCode == HttpStatusCode.NotModified)
            return new UpdateCheckResult(true, false, responseEtag, null);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
        var release = await JsonSerializer.DeserializeAsync<GitHubRelease>(stream, JsonOptions, cancellationToken)
            .ConfigureAwait(false) ?? throw new InvalidDataException("GitHub 返回了空的版本信息。");
        if (release.Draft || release.Prerelease)
            return new UpdateCheckResult(false, false, responseEtag, null);
        if (!EcosystemVersion.TryParse(release.TagName, out var releaseVersion))
            throw new InvalidDataException("GitHub Release 的版本号无法识别：" + release.TagName);

        var updateAvailable = releaseVersion > currentVersion;
        if (!updateAvailable)
            return new UpdateCheckResult(false, false, responseEtag, null);

        var package = await SelectPackageAsync(release, currentVersion, cancellationToken).ConfigureAwait(false);
        if (!Uri.TryCreate(release.HtmlUrl, UriKind.Absolute, out var page))
            throw new InvalidDataException("GitHub Release 页面地址无效。");
        var info = new EcosystemReleaseInfo(
            releaseVersion,
            EcosystemVersion.Display(releaseVersion),
            string.IsNullOrWhiteSpace(release.Name) ? release.TagName : release.Name,
            release.Body?.Trim() ?? string.Empty,
            page,
            release.PublishedAt,
            package);
        return new UpdateCheckResult(false, true, responseEtag, info);
    }

    public async Task<string> DownloadAsync(
        EcosystemReleaseInfo release,
        IProgress<UpdateDownloadProgress>? progress = null,
        CancellationToken cancellationToken = default)
    {
        var destinationRoot = GetDownloadRoot(release.DisplayVersion);
        Directory.CreateDirectory(destinationRoot);
        var destination = Path.Combine(destinationRoot, release.Package.Name);
        if (File.Exists(destination) && await VerifyFileAsync(destination, release.Package, cancellationToken).ConfigureAwait(false))
        {
            progress?.Report(new UpdateDownloadProgress(new FileInfo(destination).Length, new FileInfo(destination).Length));
            return destination;
        }

        var partial = destination + ".partial";
        try
        {
            File.Delete(partial);
            using var response = await _httpClient.GetAsync(release.Package.DownloadUri,
                HttpCompletionOption.ResponseHeadersRead, cancellationToken).ConfigureAwait(false);
            response.EnsureSuccessStatusCode();
            var total = response.Content.Headers.ContentLength ?? (release.Package.Size > 0 ? release.Package.Size : null);
            await using var input = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
            await using var output = new FileStream(partial, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1024 * 128,
                FileOptions.Asynchronous | FileOptions.SequentialScan | FileOptions.WriteThrough);
            var buffer = new byte[1024 * 128];
            long received = 0;
            while (true)
            {
                var read = await input.ReadAsync(buffer, cancellationToken).ConfigureAwait(false);
                if (read == 0) break;
                await output.WriteAsync(buffer.AsMemory(0, read), cancellationToken).ConfigureAwait(false);
                received += read;
                progress?.Report(new UpdateDownloadProgress(received, total));
            }
            await output.FlushAsync(cancellationToken).ConfigureAwait(false);
            if (!await VerifyFileAsync(partial, release.Package, cancellationToken).ConfigureAwait(false))
                throw new InvalidDataException("升级包大小或 SHA-256 校验失败，已拒绝安装。");
            File.Move(partial, destination, true);
            return destination;
        }
        finally
        {
            File.Delete(partial);
        }
    }

    public void Dispose()
    {
        if (_ownsClient) _httpClient.Dispose();
    }

    private async Task<EcosystemPackageAsset> SelectPackageAsync(
        GitHubRelease release,
        Version currentVersion,
        CancellationToken cancellationToken)
    {
        var exactMarker = $"upgrade-from-v{currentVersion.Major}.{currentVersion.Minor}.{Math.Max(0, currentVersion.Build)}";
        var candidates = release.Assets.Where(asset => asset.Name.EndsWith(".zip", StringComparison.OrdinalIgnoreCase)).ToArray();
        var selected = candidates.FirstOrDefault(asset => asset.Name.Contains(exactMarker, StringComparison.OrdinalIgnoreCase))
            ?? candidates.FirstOrDefault(asset => asset.Name.Contains("upgrade-from-v1.x", StringComparison.OrdinalIgnoreCase))
            ?? candidates.FirstOrDefault(asset => asset.Name.Contains("-full", StringComparison.OrdinalIgnoreCase))
            ?? throw new InvalidDataException("新版本没有可用的 Windows 生态升级包。");
        if (!Uri.TryCreate(selected.BrowserDownloadUrl, UriKind.Absolute, out var downloadUri))
            throw new InvalidDataException("升级包下载地址无效。");
        var digest = NormalizeDigest(selected.Digest);
        if (string.IsNullOrWhiteSpace(digest))
            digest = await LoadChecksumAsync(release.Assets, selected.Name, cancellationToken).ConfigureAwait(false);
        if (string.IsNullOrWhiteSpace(digest))
            throw new InvalidDataException("升级包没有 SHA-256 校验值，已拒绝自动安装。");
        var kind = selected.Name.Contains("upgrade", StringComparison.OrdinalIgnoreCase) ? "upgrade" : "full";
        return new EcosystemPackageAsset(selected.Name, downloadUri, digest, selected.Size, kind);
    }

    private async Task<string> LoadChecksumAsync(
        IReadOnlyList<GitHubAsset> assets,
        string packageName,
        CancellationToken cancellationToken)
    {
        var checksum = assets.FirstOrDefault(asset => asset.Name.Equals("SHA256SUMS.txt", StringComparison.OrdinalIgnoreCase));
        if (checksum is null || !Uri.TryCreate(checksum.BrowserDownloadUrl, UriKind.Absolute, out var uri)) return string.Empty;
        var text = await _httpClient.GetStringAsync(uri, cancellationToken).ConfigureAwait(false);
        foreach (var line in text.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
        {
            var parts = line.Trim().Split((char[]?)null, 2, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length != 2) continue;
            var listedName = parts[1].Trim().TrimStart('*').Replace('\\', '/');
            if (listedName.EndsWith('/' + packageName, StringComparison.OrdinalIgnoreCase) ||
                listedName.Equals(packageName, StringComparison.OrdinalIgnoreCase))
                return NormalizeDigest(parts[0]);
        }
        return string.Empty;
    }

    private static async Task<bool> VerifyFileAsync(
        string path,
        EcosystemPackageAsset package,
        CancellationToken cancellationToken)
    {
        var info = new FileInfo(path);
        if (package.Size > 0 && info.Length != package.Size) return false;
        await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1024 * 128,
            FileOptions.Asynchronous | FileOptions.SequentialScan);
        var hash = await SHA256.HashDataAsync(stream, cancellationToken).ConfigureAwait(false);
        return Convert.ToHexString(hash).Equals(package.Sha256, StringComparison.OrdinalIgnoreCase);
    }

    private static string NormalizeDigest(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return string.Empty;
        var normalized = value.Trim();
        if (normalized.StartsWith("sha256:", StringComparison.OrdinalIgnoreCase)) normalized = normalized[7..];
        return normalized.Length == 64 && normalized.All(Uri.IsHexDigit) ? normalized.ToUpperInvariant() : string.Empty;
    }

    public static string GetCacheRoot()
    {
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrWhiteSpace(local)) throw new InvalidOperationException("LOCALAPPDATA 不可用，不能安全下载更新。");
        return Path.Combine(local, "CodexFeishuEcosystemUpdates");
    }

    public static void CleanupStaleCache()
    {
        var root = GetCacheRoot();
        if (!Directory.Exists(root)) return;
        foreach (var name in new[] { "downloads", "staging", "runner" })
        {
            var target = Path.GetFullPath(Path.Combine(root, name));
            if (!target.StartsWith(Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar,
                    StringComparison.OrdinalIgnoreCase)) continue;
            try { if (Directory.Exists(target)) Directory.Delete(target, true); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        foreach (var progress in Directory.EnumerateFiles(root, "progress-*.json", SearchOption.TopDirectoryOnly))
            try { File.Delete(progress); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        var logs = Path.Combine(root, "logs");
        if (!Directory.Exists(logs)) return;
        foreach (var log in Directory.EnumerateFiles(logs, "update-*.log", SearchOption.TopDirectoryOnly))
            try { if (File.GetLastWriteTimeUtc(log) < DateTime.UtcNow.AddDays(-14)) File.Delete(log); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
    }

    private static string GetDownloadRoot(string displayVersion) =>
        Path.Combine(GetCacheRoot(), "downloads", displayVersion.TrimStart('v', 'V'));

    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNameCaseInsensitive = true };

    private sealed class GitHubRelease
    {
        [JsonPropertyName("tag_name")] public string TagName { get; set; } = string.Empty;
        [JsonPropertyName("name")] public string Name { get; set; } = string.Empty;
        [JsonPropertyName("body")] public string? Body { get; set; }
        [JsonPropertyName("html_url")] public string HtmlUrl { get; set; } = string.Empty;
        [JsonPropertyName("draft")] public bool Draft { get; set; }
        [JsonPropertyName("prerelease")] public bool Prerelease { get; set; }
        [JsonPropertyName("published_at")] public DateTimeOffset? PublishedAt { get; set; }
        [JsonPropertyName("assets")] public List<GitHubAsset> Assets { get; set; } = [];
    }

    private sealed class GitHubAsset
    {
        [JsonPropertyName("name")] public string Name { get; set; } = string.Empty;
        [JsonPropertyName("browser_download_url")] public string BrowserDownloadUrl { get; set; } = string.Empty;
        [JsonPropertyName("digest")] public string? Digest { get; set; }
        [JsonPropertyName("size")] public long Size { get; set; }
    }
}

public static class EcosystemUpdaterLauncher
{
    public static Process Launch(EcosystemInstallation installation, EcosystemReleaseInfo release, string packagePath)
    {
        if (!installation.IsManaged) throw new InvalidOperationException(installation.ManagementMessage);
        var sourceUpdater = Path.Combine(AppPaths.Root, "Ecosystem.Updater.exe");
        if (!File.Exists(sourceUpdater)) throw new FileNotFoundException("当前安装缺少独立生态更新器。", sourceUpdater);
        var runnerRoot = Path.Combine(EcosystemUpdateService.GetCacheRoot(), "runner", release.DisplayVersion.TrimStart('v', 'V'));
        Directory.CreateDirectory(runnerRoot);
        var runner = Path.Combine(runnerRoot, "Ecosystem.Updater.exe");
        File.Copy(sourceUpdater, runner, true);
        var start = new ProcessStartInfo(runner)
        {
            WorkingDirectory = runnerRoot,
            UseShellExecute = false,
        };
        start.ArgumentList.Add("--parent-pid");
        start.ArgumentList.Add(Environment.ProcessId.ToString());
        start.ArgumentList.Add("--package");
        start.ArgumentList.Add(Path.GetFullPath(packagePath));
        start.ArgumentList.Add("--install-root");
        start.ArgumentList.Add(installation.InstallRoot);
        start.ArgumentList.Add("--sha256");
        start.ArgumentList.Add(release.Package.Sha256);
        start.ArgumentList.Add("--version");
        start.ArgumentList.Add(release.DisplayVersion.TrimStart('v', 'V'));
        return Process.Start(start) ?? throw new InvalidOperationException("无法启动独立生态更新器。");
    }
}
