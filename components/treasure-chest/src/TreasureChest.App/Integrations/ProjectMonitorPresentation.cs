namespace TreasureChest.Integrations;

public enum CatalogMonitoringStatus
{
    Unmonitored,
    LongTerm,
    Temporary,
}

/// <summary>
/// 项目监测在百宝箱中的统一排序与显示规则。
/// </summary>
public static class ProjectMonitorPresentation
{
    public static bool IsCatalogVisible(CodexThreadInfo item, bool includeArchived) => includeArchived || !item.Archived;

    public static string CatalogCountTitle(int visibleCount, int catalogCount) =>
        $"全部会话（当前显示 {visibleCount} / 目录 {catalogCount}）";

    public static CatalogMonitoringStatus MonitoringStatus(CodexThreadInfo item, IEnumerable<ProjectMonitorItem> monitors)
    {
        var monitor = monitors.FirstOrDefault(candidate => candidate.ThreadId.Equals(item.Id, StringComparison.OrdinalIgnoreCase));
        if (monitor is null) return CatalogMonitoringStatus.Unmonitored;
        return monitor.IsManual ? CatalogMonitoringStatus.LongTerm : CatalogMonitoringStatus.Temporary;
    }

    public static string MonitoringStatusText(CatalogMonitoringStatus status) => status switch
    {
        CatalogMonitoringStatus.LongTerm => "长期监测",
        CatalogMonitoringStatus.Temporary => "临时监测",
        _ => "未监测",
    };

    public static IReadOnlyList<ProjectMonitorItem> OrderMonitors(IEnumerable<ProjectMonitorItem> items) =>
        items.OrderBy(item => item.IsManual ? 0 : 1)
            .ThenBy(item => IsPersonal(item.Classification) ? 1 : 0)
            .ThenBy(item => NormalizeClassification(item.Classification), StringComparer.CurrentCultureIgnoreCase)
            .ThenByDescending(item => item.LastActivityAt ?? DateTimeOffset.MinValue)
            .ThenBy(item => item.Title, StringComparer.CurrentCultureIgnoreCase)
            .ThenBy(item => item.ThreadId, StringComparer.OrdinalIgnoreCase)
            .ToArray();

    public static IReadOnlyList<ProjectMonitorItem> OrderWithinOrigin(IEnumerable<ProjectMonitorItem> items) =>
        items.OrderBy(item => IsPersonal(item.Classification) ? 1 : 0)
            .ThenBy(item => NormalizeClassification(item.Classification), StringComparer.CurrentCultureIgnoreCase)
            .ThenByDescending(item => item.LastActivityAt ?? DateTimeOffset.MinValue)
            .ThenBy(item => item.Title, StringComparer.CurrentCultureIgnoreCase)
            .ThenBy(item => item.ThreadId, StringComparer.OrdinalIgnoreCase)
            .ToArray();

    public static IReadOnlyList<CodexThreadInfo> OrderCatalog(IEnumerable<CodexThreadInfo> items) =>
        items.OrderBy(item => IsPersonal(item.Classification) ? 1 : 0)
            .ThenBy(item => NormalizeClassification(item.Classification), StringComparer.CurrentCultureIgnoreCase)
            .ThenByDescending(item => item.UpdatedAtMs ?? 0)
            .ThenBy(item => item.DisplayTitle, StringComparer.CurrentCultureIgnoreCase)
            .ThenBy(item => item.Id, StringComparer.OrdinalIgnoreCase)
            .ToArray();

    public static bool IsPersonal(string? classification)
    {
        if (string.IsNullOrWhiteSpace(classification)) return true;
        var value = classification.Trim();
        return value.Equals("个人对话", StringComparison.OrdinalIgnoreCase) ||
            value.Equals("个人会话", StringComparison.OrdinalIgnoreCase);
    }

    public static string NormalizeClassification(string? classification) =>
        IsPersonal(classification) ? "个人对话" : classification!.Trim();

    public static string GroupTitle(string classification, int count) =>
        IsPersonal(classification)
            ? $"个人对话（{count}）"
            : $"项目 · {NormalizeClassification(classification)}（{count}）";

    public static string DrawerGroupTitle(string classification, int count, bool expanded)
    {
        _ = expanded;
        return GroupTitle(classification, count);
    }

    public static string FormatTimestamp(DateTimeOffset? value) =>
        value?.ToLocalTime().ToString("yyyy-MM-dd HH:mm") ?? "—";

    public static string FormatCatalogTimestamp(long? value) =>
        value.HasValue ? DateTimeOffset.FromUnixTimeMilliseconds(value.Value).ToLocalTime().ToString("yyyy-MM-dd HH:mm") : "—";

    public static string FormatRemaining(ProjectMonitorItem item)
    {
        if (item.IsManual) return "长期有效";
        if (!item.ExpiresAt.HasValue) return "未提供";
        var remaining = item.ExpiresAt.Value - DateTimeOffset.Now;
        if (remaining <= TimeSpan.Zero) return "已到期";
        if (remaining.TotalDays >= 1) return $"{(int)remaining.TotalDays}天 {remaining.Hours}小时";
        if (remaining.TotalHours >= 1) return $"{(int)remaining.TotalHours}小时 {remaining.Minutes}分";
        return $"{Math.Max(1, (int)Math.Ceiling(remaining.TotalMinutes))}分钟";
    }
}
