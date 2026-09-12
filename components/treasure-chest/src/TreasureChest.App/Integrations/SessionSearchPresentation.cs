namespace TreasureChest.Integrations;

public enum SessionSearchMode
{
    Exact,
    Fuzzy,
}

public static class SessionSearchPresentation
{
    public const int ResultsPerPage = 3;

    public static (bool ExactChecked, bool FuzzyChecked) ModeChecks(SessionSearchMode mode) =>
        mode == SessionSearchMode.Exact ? (true, false) : (false, true);

    public static string NormalizeExactProjectName(string? value) => (value ?? string.Empty).Trim();

    public static string ModeHint(SessionSearchMode mode) => mode == SessionSearchMode.Exact
        ? "精确搜索按修剪后的完整项目名称匹配。"
        : "请填写会话名称、描述或最后活动时间；任意条件都可留空。";

    public static bool ExactProjectMatches(string? classification, string? query)
    {
        var expected = NormalizeExactProjectName(query);
        if (expected.Length == 0) return true;
        if (ProjectMonitorPresentation.IsPersonal(classification)) return false;
        return ProjectMonitorPresentation.NormalizeClassification(classification)
            .Equals(expected, StringComparison.CurrentCultureIgnoreCase);
    }

    public static bool HasExactProject(IEnumerable<string> classifications, string? query)
    {
        var expected = NormalizeExactProjectName(query);
        return expected.Length == 0 || classifications.Any(value => ExactProjectMatches(value, expected));
    }

    public static IReadOnlyList<SessionSearchMatch> OrderMatches(IEnumerable<SessionSearchMatch> matches) =>
        matches.OrderByDescending(item => item.Score).ThenBy(item => item.ThreadId, StringComparer.OrdinalIgnoreCase).ToArray();

    public static int PageCount(int matchCount) => Math.Max(1, (Math.Max(0, matchCount) + ResultsPerPage - 1) / ResultsPerPage);

    public static IReadOnlyList<SessionSearchMatch> Page(IEnumerable<SessionSearchMatch> matches, int pageIndex) =>
        OrderMatches(matches).Skip(Math.Max(0, pageIndex) * ResultsPerPage).Take(ResultsPerPage).ToArray();

    public static IReadOnlyList<CodexThreadInfo> OrderCatalogWithMatches(
        IEnumerable<CodexThreadInfo> catalog,
        IEnumerable<SessionSearchMatch> matches)
    {
        var rank = OrderMatches(matches).Select((item, index) => (item.ThreadId, index))
            .GroupBy(item => item.ThreadId, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(group => group.Key, group => group.Min(item => item.index), StringComparer.OrdinalIgnoreCase);
        var baseOrder = ProjectMonitorPresentation.OrderCatalog(catalog);
        if (rank.Count == 0) return baseOrder;
        return baseOrder
            .GroupBy(item => ProjectMonitorPresentation.NormalizeClassification(item.Classification), StringComparer.CurrentCultureIgnoreCase)
            .OrderBy(group => group.Where(item => rank.ContainsKey(item.Id)).Select(item => rank[item.Id]).DefaultIfEmpty(int.MaxValue).Min())
            .ThenBy(group => ProjectMonitorPresentation.IsPersonal(group.Key) ? 1 : 0)
            .ThenBy(group => group.Key, StringComparer.CurrentCultureIgnoreCase)
            .SelectMany(group => group.OrderBy(item => rank.TryGetValue(item.Id, out var value) ? value : int.MaxValue)
                .ThenByDescending(item => item.UpdatedAtMs ?? long.MinValue))
            .ToArray();
    }

    public static int CenteredFirstRow(int targetRow, int displayedRows, int totalRows)
    {
        if (totalRows <= 0) return 0;
        var visible = Math.Max(1, displayedRows);
        return Math.Clamp(targetRow - visible / 2, 0, Math.Max(0, totalRows - visible));
    }

    public static string ResultHeading(string status) => status switch
    {
        "found" => "找到最可能的会话",
        "ambiguous" => "找到多个候选会话",
        "not_found" => "没有找到",
        _ => "会话搜索结果",
    };

    public static string MonitorText(SessionSearchMonitor monitor) => !monitor.Monitored
        ? "未监测"
        : monitor.Origin == "manual" ? "长期监测" : "临时监测";
}
