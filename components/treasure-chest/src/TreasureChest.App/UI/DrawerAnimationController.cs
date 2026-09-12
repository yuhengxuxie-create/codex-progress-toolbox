using System.Runtime.CompilerServices;

namespace TreasureChest.UI;

public static class DrawerAnimationController
{
    private sealed record RowState(DataGridViewRow Row, int FullHeight, Color FullForeColor);

    private sealed class DrawerState
    {
        public required RowState[] Rows { get; init; }
        public double Progress { get; set; }
        public bool TargetExpanded { get; set; }
    }

    private static readonly ConditionalWeakTable<DataGridView, Dictionary<string, DrawerState>> Active = new();

    public static bool IsToggleGesture(MouseButtons button, int clickCount) =>
        button == MouseButtons.Left && clickCount >= 1;

    public static bool NextExpandedState(bool currentlyExpanded) => !currentlyExpanded;

    public static void Expand(
        DataGridView grid,
        string groupKey,
        IReadOnlyList<DataGridViewRow> rows,
        Action? completed = null)
    {
        if (rows.Count == 0)
        {
            completed?.Invoke();
            return;
        }
        var map = StatesFor(grid);
        if (!map.TryGetValue(groupKey, out var state) || !RowsMatch(state.Rows, rows))
        {
            state = new DrawerState
            {
                Rows = CaptureFullRows(rows),
                Progress = 0D,
                TargetExpanded = true,
            };
            map[groupKey] = state;
            Apply(state, grid, 0D);
        }
        Animate(grid, groupKey, state, target: 1D, completed);
    }

    /// <summary>
    /// Reverses an in-flight collapse on the same row instances. Callers use this
    /// before rebuilding their data source, preserving the current visual height.
    /// </summary>
    public static bool TryReverseToExpanded(
        DataGridView grid,
        string groupKey,
        IReadOnlyList<DataGridViewRow> rows,
        Action? completed = null)
    {
        var map = StatesFor(grid);
        if (!map.TryGetValue(groupKey, out var state) || state.TargetExpanded || !RowsMatch(state.Rows, rows))
            return false;
        Animate(grid, groupKey, state, target: 1D, completed);
        return true;
    }

    public static void Collapse(
        DataGridView grid,
        string groupKey,
        IReadOnlyList<DataGridViewRow> rows,
        Action completed)
    {
        if (rows.Count == 0)
        {
            completed();
            return;
        }
        var map = StatesFor(grid);
        if (!map.TryGetValue(groupKey, out var state) || !RowsMatch(state.Rows, rows))
        {
            state = new DrawerState
            {
                Rows = CaptureFullRows(rows),
                Progress = 1D,
                TargetExpanded = false,
            };
            map[groupKey] = state;
        }
        Animate(grid, groupKey, state, target: 0D, completed);
    }

    public static double? VisualProgress(DataGridView grid, string groupKey) =>
        StatesFor(grid).TryGetValue(groupKey, out var state) ? state.Progress : null;

    public static IReadOnlyList<DataGridViewRow> ChildRowsAfter(DataGridView grid, int headerIndex)
    {
        var rows = new List<DataGridViewRow>();
        for (var index = headerIndex + 1; index < grid.Rows.Count; index++)
        {
            var row = grid.Rows[index];
            if (row.Tag is null || row.Tag.GetType().Name.Contains("DrawerGroup", StringComparison.Ordinal)) break;
            rows.Add(row);
        }
        return rows;
    }

    private static Dictionary<string, DrawerState> StatesFor(DataGridView grid) =>
        Active.GetValue(grid, _ => new Dictionary<string, DrawerState>(StringComparer.CurrentCultureIgnoreCase));

    private static RowState[] CaptureFullRows(IReadOnlyList<DataGridViewRow> rows) =>
        rows.Select(row => new RowState(row, row.Height, row.DefaultCellStyle.ForeColor)).ToArray();

    private static bool RowsMatch(IReadOnlyList<RowState> states, IReadOnlyList<DataGridViewRow> rows) =>
        states.Count == rows.Count && states.Select(state => state.Row).Zip(rows, ReferenceEquals).All(value => value);

    private static void Animate(
        DataGridView grid,
        string groupKey,
        DrawerState state,
        double target,
        Action? completed)
    {
        var map = StatesFor(grid);
        map[groupKey] = state;
        var start = state.Progress;
        state.TargetExpanded = target >= 0.5D;
        var distance = Math.Abs(target - start);
        var duration = Math.Max(1, (int)Math.Round(AnimationTokens.ComplexDurationMs * distance));
        AnimationRunner.Start(grid, "drawer:" + groupKey, duration, eased =>
        {
            state.Progress = start + (target - start) * eased;
            Apply(state, grid, state.Progress);
        }, () =>
        {
            state.Progress = target;
            Apply(state, grid, target);
            if (map.TryGetValue(groupKey, out var active) && ReferenceEquals(active, state)) map.Remove(groupKey);
            completed?.Invoke();
        });
    }

    private static void Apply(DrawerState state, DataGridView grid, double progress)
    {
        progress = Math.Clamp(progress, 0D, 1D);
        var changed = false;
        foreach (var rowState in state.Rows)
        {
            if (rowState.Row.DataGridView is null) continue;
            var minimumHeight = progress >= 1D ? 3 : 2;
            var height = Math.Max(2,
                (int)Math.Round(2 + (rowState.FullHeight - 2) * progress));
            var foreColor = progress >= 1D
                ? rowState.FullForeColor
                : Blend(UiTheme.Surface, rowState.FullForeColor, progress);
            if (rowState.Row.MinimumHeight != minimumHeight)
            {
                rowState.Row.MinimumHeight = minimumHeight;
                changed = true;
            }
            if (rowState.Row.Height != height)
            {
                rowState.Row.Height = height;
                changed = true;
            }
            if (rowState.Row.DefaultCellStyle.ForeColor != foreColor)
            {
                rowState.Row.DefaultCellStyle.ForeColor = foreColor;
                changed = true;
            }
        }
        if (changed) grid.Invalidate();
    }

    private static Color Blend(Color from, Color to, double amount)
    {
        amount = Math.Clamp(amount, 0D, 1D);
        return Color.FromArgb(
            (int)Math.Round(from.R + (to.R - from.R) * amount),
            (int)Math.Round(from.G + (to.G - from.G) * amount),
            (int)Math.Round(from.B + (to.B - from.B) * amount));
    }
}
