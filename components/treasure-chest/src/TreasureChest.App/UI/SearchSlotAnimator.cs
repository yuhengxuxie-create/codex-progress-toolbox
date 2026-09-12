using System.Drawing.Drawing2D;

namespace TreasureChest.UI;

public readonly record struct SearchSlotFrameGeometry(
    Rectangle GlyphBounds,
    Rectangle TextBounds,
    Rectangle RevealBounds,
    Rectangle FeatherBounds,
    double OldOpacity);

public static class SearchSlotAnimator
{
    private sealed class TransitionState
    {
        public required TextBox Input { get; init; }
        public required SearchSlotOverlay Overlay { get; init; }
        public required string OldText { get; init; }
        public required string NewText { get; init; }
        public required ButtonGlyph OldGlyph { get; init; }
        public required ButtonGlyph NewGlyph { get; init; }
        public double Progress { get; set; }
        public Action? Completed { get; set; }
    }

    private static readonly Dictionary<TextBox, TransitionState> Active = new(ReferenceEqualityComparer.Instance);

    public static void Transition(
        TextBox input,
        string oldText,
        string newText,
        ButtonGlyph oldGlyph,
        ButtonGlyph newGlyph,
        Action? completed = null)
    {
        if (!AnimationTokens.Enabled || !input.IsHandleCreated ||
            !MonitorToolbarPanel.TryGetSearchHost(input, out var host, out var textBounds))
        {
            RemoveExisting(input);
            input.Text = newText;
            MonitorToolbarPanel.SetSearchGlyph(input, newGlyph);
            completed?.Invoke();
            return;
        }

        if (Active.TryGetValue(input, out var active))
        {
            var target = Endpoint(active, newText, newGlyph);
            if (target.HasValue)
            {
                input.Text = newText;
                active.Completed = completed;
                AnimateTo(active, target.Value);
                return;
            }
            RemoveExisting(input);
        }

        var overlay = new SearchSlotOverlay(input, oldText, newText, oldGlyph, newGlyph, textBounds)
        {
            Bounds = host.ClientRectangle,
            Font = input.Font,
            Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right,
        };
        var state = new TransitionState
        {
            Input = input,
            Overlay = overlay,
            OldText = oldText,
            NewText = newText,
            OldGlyph = oldGlyph,
            NewGlyph = newGlyph,
            Progress = 0D,
            Completed = completed,
        };
        Active[input] = state;
        input.Disposed += InputDisposed;
        input.Text = newText;
        host.Controls.Add(overlay);
        overlay.BringToFront();
        overlay.SetProgress(0D);
        AnimateTo(state, 1D);
    }

    public static SearchSlotFrameGeometry FrameGeometry(
        Rectangle hostBounds,
        Rectangle textBounds,
        double progress,
        int dpi)
    {
        progress = Math.Clamp(progress, 0D, 1D);
        var iconSize = DpiLayout.Scale(20, dpi);
        var glyph = new Rectangle(
            hostBounds.Left + DpiLayout.Scale(14, dpi),
            hostBounds.Top + (hostBounds.Height - iconSize) / 2,
            iconSize,
            iconSize);
        var revealWidth = (int)Math.Round(hostBounds.Width * progress);
        var reveal = new Rectangle(hostBounds.Left, hostBounds.Top,
            Math.Clamp(revealWidth, 0, hostBounds.Width), hostBounds.Height);
        var featherWidth = Math.Min(DpiLayout.Scale(34, dpi), reveal.Width);
        var feather = featherWidth <= 0
            ? Rectangle.Empty
            : new Rectangle(reveal.Right - featherWidth, reveal.Top, featherWidth, reveal.Height);
        return new SearchSlotFrameGeometry(glyph, textBounds, reveal, feather, 1D - progress);
    }

    internal static Rectangle SearchGlyphBounds(Rectangle hostBounds, int dpi) =>
        FrameGeometry(hostBounds, Rectangle.Empty, 0D, dpi).GlyphBounds;

    internal static void DrawGlyph(Graphics graphics, ButtonGlyph glyph, Rectangle bounds, Color color, int dpi) =>
        ThemeGlyphRenderer.Draw(graphics, ThemeGlyphMap.From(glyph), bounds, color);

    private static double? Endpoint(TransitionState state, string text, ButtonGlyph glyph)
    {
        if (state.NewText == text && state.NewGlyph == glyph) return 1D;
        if (state.OldText == text && state.OldGlyph == glyph) return 0D;
        return null;
    }

    private static void AnimateTo(TransitionState state, double target)
    {
        var start = state.Progress;
        var distance = Math.Abs(target - start);
        var duration = Math.Max(1, (int)Math.Round(AnimationTokens.StandardDurationMs * distance));
        AnimationRunner.Start(state.Input, "mode-content", duration, eased =>
        {
            state.Progress = start + (target - start) * eased;
            state.Overlay.SetProgress(state.Progress);
        }, () => Finish(state, target));
    }

    private static void Finish(TransitionState state, double endpoint)
    {
        if (!Active.TryGetValue(state.Input, out var current) || !ReferenceEquals(current, state)) return;
        state.Progress = endpoint;
        var text = endpoint >= 0.5D ? state.NewText : state.OldText;
        var glyph = endpoint >= 0.5D ? state.NewGlyph : state.OldGlyph;
        var completed = state.Completed;
        state.Input.Text = text;
        MonitorToolbarPanel.SetSearchGlyph(state.Input, glyph);
        RemoveExisting(state.Input, cancelTimeline: false);
        completed?.Invoke();
    }

    private static void InputDisposed(object? sender, EventArgs e)
    {
        if (sender is TextBox input) RemoveExisting(input);
    }

    private static void RemoveExisting(TextBox input, bool cancelTimeline = true)
    {
        if (cancelTimeline) AnimationRunner.Cancel(input, "mode-content");
        if (!Active.Remove(input, out var state)) return;
        input.Disposed -= InputDisposed;
        state.Overlay.Parent?.Controls.Remove(state.Overlay);
        state.Overlay.Dispose();
    }

    private sealed class SearchSlotOverlay : Control
    {
        private readonly TextBox _animationOwner;
        private readonly string _oldText;
        private readonly string _newText;
        private readonly ButtonGlyph _oldGlyph;
        private readonly ButtonGlyph _newGlyph;
        private readonly Rectangle _textBounds;
        private double _progress;

        public SearchSlotOverlay(
            TextBox animationOwner,
            string oldText,
            string newText,
            ButtonGlyph oldGlyph,
            ButtonGlyph newGlyph,
            Rectangle textBounds)
        {
            _animationOwner = animationOwner;
            _oldText = oldText;
            _newText = newText;
            _oldGlyph = oldGlyph;
            _newGlyph = newGlyph;
            _textBounds = textBounds;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.ResizeRedraw | ControlStyles.UserPaint, true);
        }

        public void SetProgress(double progress)
        {
            _progress = Math.Clamp(progress, 0D, 1D);
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.Clear(Parent?.Parent?.BackColor ?? UiTheme.Surface);
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using var path = RoundedButton.RoundedPath(ClientRectangle, DpiLayout.Scale(11, DeviceDpi));
            using (var fill = new SolidBrush(UiTheme.Surface)) e.Graphics.FillPath(fill, path);
            using (var border = new Pen(UiTheme.Border, 1F)) e.Graphics.DrawPath(border, path);

            var geometry = FrameGeometry(ClientRectangle, _textBounds, _progress, DeviceDpi);
            var oldColor = Color.FromArgb((int)Math.Round(255D * geometry.OldOpacity), UiTheme.Text);
            DrawContent(e.Graphics, _oldText, _oldGlyph, geometry, oldColor);

            if (geometry.RevealBounds.Width > 0)
            {
                var state = e.Graphics.Save();
                e.Graphics.SetClip(geometry.RevealBounds);
                DrawContent(e.Graphics, _newText, _newGlyph, geometry, UiTheme.Text);
                if (geometry.FeatherBounds.Width > 1)
                {
                    using var gradient = new LinearGradientBrush(geometry.FeatherBounds,
                        Color.FromArgb(96, UiTheme.Surface), Color.Transparent, LinearGradientMode.Horizontal);
                    e.Graphics.FillRectangle(gradient, geometry.FeatherBounds);
                }
                e.Graphics.Restore(state);
            }
            AnimationRunner.ReportPaint(_animationOwner);
        }

        private void DrawContent(
            Graphics graphics,
            string text,
            ButtonGlyph glyph,
            SearchSlotFrameGeometry geometry,
            Color color)
        {
            DrawGlyph(graphics, glyph, geometry.GlyphBounds, color, DeviceDpi);
            ThemePaint.DrawText(graphics, text, Font, geometry.TextBounds, color,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis |
                TextFormatFlags.NoPadding | TextFormatFlags.SingleLine);
        }
    }
}
