using System.Globalization;
using System.Net;
using System.Text;
using System.Text.Json;

namespace TreasureChest.Services;

public sealed class UpdateRateLimitException(DateTimeOffset retryAt)
    : HttpRequestException($"GitHub 更新查询暂时限流。最早可在 {retryAt.ToLocalTime():yyyy-MM-dd HH:mm:ss zzz} 重试；也可点击“打开官方发布页”手动下载。")
{
    public DateTimeOffset RetryAt { get; } = retryAt;
}

public sealed partial class EcosystemUpdateService
{
    public const string ManualReleasePage = RepositoryWebRoot + "/releases/latest";
    private readonly SemaphoreSlim _checkGate = new(1, 1);
    private readonly Func<DateTimeOffset> _utcNow;
    private readonly string? _checkStatePath;
    private CheckState _checkState = new();
    private UpdateCheckResult? _recentCheck;
    private Version? _recentVersion;
    private DateTimeOffset _recentAt;

    private sealed class CheckState
    {
        public DateTimeOffset ApiRetryAt { get; set; }
        public DateTimeOffset ManifestRetryAt { get; set; }
        public int Failures { get; set; }
    }

    public async Task<UpdateCheckResult> CheckAsync(Version currentVersion, string? etag = null,
        CancellationToken cancellationToken = default)
    {
        await _checkGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var now = _utcNow();
            if (_recentCheck is { NotModified: false } && _recentVersion == currentVersion &&
                now >= _recentAt && now - _recentAt < TimeSpan.FromMinutes(5)) return _recentCheck;
            UpdateCheckResult result;
            try
            {
                if (_checkState.ApiRetryAt > now) throw new UpdateRateLimitException(_checkState.ApiRetryAt);
                result = await CheckCoreAsync(currentVersion, etag, cancellationToken).ConfigureAwait(false);
                _checkState.ApiRetryAt = default;
                _checkState.Failures = 0;
                SaveCheckState();
            }
            catch (UpdateRateLimitException limited)
            {
                // The fallback is a public release asset, not another REST API request.
                if (_checkState.ManifestRetryAt > _utcNow()) throw;
                _checkState.ManifestRetryAt = AddBounded(_utcNow(), 300);
                SaveCheckState();
                try { result = await CheckManifestAsync(currentVersion, cancellationToken).ConfigureAwait(false); }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested) { throw; }
                catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException or JsonException or
                    InvalidOperationException or FormatException or KeyNotFoundException or OperationCanceledException)
                {
                    var wait = error is UpdateRateLimitException manifestLimit ? manifestLimit : limited;
                    throw new HttpRequestException(wait.Message + " 官方更新清单暂不可用，请使用发布页。", error);
                }
            }
            _recentAt = _utcNow();
            _recentVersion = currentVersion;
            _recentCheck = result;
            return result;
        }
        finally { _checkGate.Release(); }
    }

    private async Task ThrowIfRateLimitedAsync(HttpResponseMessage response, CancellationToken token, bool manifest = false)
    {
        if (response.StatusCode is not (HttpStatusCode.Forbidden or HttpStatusCode.TooManyRequests)) return;
        var remainingZero = response.Headers.TryGetValues("X-RateLimit-Remaining", out var remaining) && remaining.Contains("0");
        var retryPresent = response.Headers.Contains("Retry-After");
        var limited = response.StatusCode == HttpStatusCode.TooManyRequests || remainingZero || retryPresent;
        if (!limited)
        {
            var body = await ReadBoundedTextAsync(response, 8192, token).ConfigureAwait(false);
            limited = body.Contains("rate limit", StringComparison.OrdinalIgnoreCase);
        }
        if (!limited) return; // A permissions/access 403 is not silently reclassified as quota exhaustion.
        var now = _utcNow(); // Retry-After is measured after the response arrives.
        var retry = now;
        if (response.Headers.TryGetValues("Retry-After", out var values))
            foreach (var value in values)
            {
                if (TrySeconds(value, out var seconds))
                    retry = Later(retry, AddBounded(now, seconds));
                else if (DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var date))
                    retry = Later(retry, date);
            }
        if (response.Headers.TryGetValues("X-RateLimit-Reset", out var resets))
            foreach (var value in resets)
                if (TrySeconds(value, out var seconds))
                    retry = Later(retry, AddBounded(DateTimeOffset.UnixEpoch, seconds));
        _checkState.Failures = Math.Min(_checkState.Failures + 1, 7);
        if (retry <= now) retry = AddBounded(now, 60 * (decimal)Math.Pow(2, _checkState.Failures - 1));
        if (manifest) _checkState.ManifestRetryAt = Later(_checkState.ManifestRetryAt, retry);
        else _checkState.ApiRetryAt = Later(_checkState.ApiRetryAt, retry);
        SaveCheckState();
        throw new UpdateRateLimitException(manifest ? _checkState.ManifestRetryAt : _checkState.ApiRetryAt);
    }

    private static DateTimeOffset Later(DateTimeOffset a, DateTimeOffset b) => a > b ? a : b;
    private static bool TrySeconds(string value, out decimal seconds)
    {
        seconds = 0;
        if (value.Length == 0 || !value.All(c => c is >= '0' and <= '9')) return false;
        // Valid positive integers larger than decimal capacity must not shorten the server's wait.
        if (!decimal.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out seconds)) seconds = decimal.MaxValue;
        return true;
    }
    private static DateTimeOffset AddBounded(DateTimeOffset start, decimal seconds)
    {
        if (seconds < 0) return start;
        var available = (decimal)(DateTimeOffset.MaxValue - start).Ticks / TimeSpan.TicksPerSecond;
        return seconds >= available ? DateTimeOffset.MaxValue : start.AddTicks((long)(seconds * TimeSpan.TicksPerSecond));
    }

    private async Task<UpdateCheckResult> CheckManifestAsync(Version currentVersion, CancellationToken token)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, ManualReleasePage + "/download/ecosystem-update.json");
        request.Headers.Accept.Clear();
        request.Headers.Accept.ParseAdd("application/json");
        using var response = await _httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, token).ConfigureAwait(false);
        await ThrowIfRateLimitedAsync(response, token, manifest: true).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        using var document = JsonDocument.Parse(await ReadBoundedTextAsync(response, 65536, token).ConfigureAwait(false));
        var root = document.RootElement;
        var text = root.GetProperty("ecosystem_version").GetString();
        if (root.GetProperty("schema_version").GetInt32() != 1 ||
            !Version.TryParse(text, out var version) || version.Build < 0 || version.Revision >= 0 ||
            text != $"{version.Major}.{version.Minor}.{version.Build}")
            throw new InvalidDataException("官方更新清单版本无效。");
        var tag = "v" + text;
        if (root.GetProperty("release_tag").GetString() != tag ||
            root.GetProperty("release_notes_url").GetString() != RepositoryWebRoot + "/releases/tag/" + tag)
            throw new InvalidDataException("官方更新清单版本来源不一致。");
        if (version <= currentVersion) return new UpdateCheckResult(false, false, string.Empty, null);
        if (!Version.TryParse(root.GetProperty("minimum_upgradable_version").GetString(), out var minimum) ||
            currentVersion < minimum)
            throw new InvalidDataException("当前版本需要按官方发布页的说明手动升级。");
        var package = root.GetProperty("packages").GetProperty("upgrade");
        var name = $"codex-feishu-ecosystem-{tag}-upgrade-from-v1.x.zip";
        var url = RepositoryWebRoot + "/releases/download/" + tag + "/" + name;
        var digest = NormalizeDigest(package.GetProperty("sha256").GetString());
        var size = package.GetProperty("size").GetInt64();
        if (package.GetProperty("name").GetString() != name || package.GetProperty("url").GetString() != url ||
            digest.Length != 64 || size <= 0)
            throw new InvalidDataException("官方更新清单包地址或校验值无效，已拒绝自动安装。");
        return new UpdateCheckResult(false, true, string.Empty,
            new EcosystemReleaseInfo(version, tag, "生态更新 " + tag,
                "GitHub API 暂时限流，版本信息来自官方 Release 更新清单。更新说明请查看官方发布页。",
                new Uri(RepositoryWebRoot + "/releases/tag/" + tag), null,
                new EcosystemPackageAsset(name, new Uri(url), digest, size, "upgrade")));
    }

    private static async Task<string> ReadBoundedTextAsync(HttpResponseMessage response, int maximum, CancellationToken token)
    {
        await using var stream = await response.Content.ReadAsStreamAsync(token).ConfigureAwait(false);
        using var buffer = new MemoryStream();
        var bytes = new byte[4096];
        while (true)
        {
            var read = await stream.ReadAsync(bytes, token).ConfigureAwait(false);
            if (read == 0) break;
            if (buffer.Length + read > maximum) throw new InvalidDataException("更新响应超过安全大小限制。");
            buffer.Write(bytes, 0, read);
        }
        return Encoding.UTF8.GetString(buffer.ToArray()).TrimStart('\uFEFF');
    }

    private void LoadCheckState()
    {
        if (_checkStatePath is null) return;
        try
        {
            if (File.Exists(_checkStatePath) && new FileInfo(_checkStatePath).Length <= 4096)
                _checkState = JsonSerializer.Deserialize<CheckState>(File.ReadAllText(_checkStatePath)) ?? new();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { }
        _checkState.Failures = Math.Clamp(_checkState.Failures, 0, 7);
    }

    private void SaveCheckState()
    {
        if (_checkStatePath is null) return;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(_checkStatePath))!);
            var temp = _checkStatePath + ".tmp";
            File.WriteAllText(temp, JsonSerializer.Serialize(_checkState));
            File.Move(temp, _checkStatePath, true);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
    }
}
