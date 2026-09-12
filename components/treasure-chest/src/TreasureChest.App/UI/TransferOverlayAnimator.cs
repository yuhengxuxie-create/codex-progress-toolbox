using System.Drawing.Drawing2D;

namespace TreasureChest.UI;

public enum MonitorTransferBucket
{
    AllSessions,
    LongTerm,
    Temporary,
}

public static class TransferOverlayAnimator
{
    public static bool ShouldAnimate(MonitorTransferBucket source, MonitorTransferBucket target) =>
        source == MonitorTransferBucket.AllSessions && target == MonitorTransferBucket.LongTerm ||
        source == MonitorTransferBucket.LongTerm && target == MonitorTransferBucket.AllSessions;

    public static async Task PlayAsync(Form owner, Rectangle sourceScreen, Rectangle targetScreen, CancellationToken cancellationToken)
    {
        if (owner.IsDisposed || sourceScreen.Width <= 0 || sourceScreen.Height <= 0 ||
            targetScreen.Width <= 0 || targetScreen.Height <= 0) return;
        using var overlay = new TransferOverlay(owner.RectangleToClient(sourceScreen), owner.RectangleToClient(targetScreen))
        {
            Dock = DockStyle.Fill,
        };
        owner.Controls.Add(overlay);
        overlay.BringToFront();
        try
        {
            await AnimationRunner.RunAsync(overlay, "transfer", AnimationTokens.ComplexDurationMs,
                overlay.SetProgress, cancellationToken);
        }
        finally
        {
            if (!owner.IsDisposed) owner.Controls.Remove(overlay);
        }
    }

    private sealed class TransferOverlay : Control, IExplicitAnimationPaintSource
    {
        private readonly RectangleF _source;
        private readonly RectangleF _target;
        private double _progress;

        public TransferOverlay(Rectangle source, Rectangle target)
        {
            _source = source;
            _target = target;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.ResizeRedraw | ControlStyles.SupportsTransparentBackColor |
                     ControlStyles.UserPaint, true);
            BackColor = Color.Transparent;
            Enabled = false;
        }

        public void SetProgress(double progress)
        {
            _progress = Math.Clamp(progress, 0D, 1D);
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            var sourceCenter = Center(_source);
            var targetCenter = Center(_target);
            var lift = Math.Max(42F, Math.Abs(targetCenter.X - sourceCenter.X) * 0.16F);
            var controlA = new PointF(sourceCenter.X + (targetCenter.X - sourceCenter.X) * 0.28F, sourceCenter.Y - lift);
            var controlB = new PointF(sourceCenter.X + (targetCenter.X - sourceCenter.X) * 0.72F, targetCenter.Y - lift);
            var center = Bezier(sourceCenter, controlA, controlB, targetCenter, (float)_progress);

            var compression = _progress < 0.24D
                ? 1D - 0.22D * (_progress / 0.24D)
                : 0.78D - 0.24D * ((_progress - 0.24D) / 0.76D);
            var width = Math.Max(42F, Math.Min(250F, _source.Width) * (float)compression);
            var height = Math.Max(22F, Math.Min(42F, _source.Height) * (float)compression);
            var card = new RectangleF(center.X - width / 2F, center.Y - height / 2F, width, height);
            var alpha = (int)Math.Round(235D * (1D - Math.Pow(_progress, 2.2D)));
            using var path = Rounded(card, height / 2F);
            using var shadow = new SolidBrush(Color.FromArgb(Math.Min(72, alpha), 20, 19, 36));
            using var fill = new SolidBrush(Color.FromArgb(alpha, UiTheme.Accent));
            var shadowCard = card;
            shadowCard.Offset(0, 3);
            using var shadowPath = Rounded(shadowCard, height / 2F);
            e.Graphics.FillPath(shadow, shadowPath);
            e.Graphics.FillPath(fill, path);

            using var dot = new SolidBrush(Color.FromArgb(alpha, UiTheme.OnAccent));
            var dotRadius = Math.Max(2F, height * 0.08F);
            for (var index = -1; index <= 1; index++)
                e.Graphics.FillEllipse(dot, center.X + index * dotRadius * 3F - dotRadius,
                    center.Y - dotRadius, dotRadius * 2F, dotRadius * 2F);
            AnimationRunner.ReportPaint(this);
        }

        private static PointF Center(RectangleF rectangle) => new(rectangle.Left + rectangle.Width / 2F, rectangle.Top + rectangle.Height / 2F);

        private static PointF Bezier(PointF p0, PointF p1, PointF p2, PointF p3, float t)
        {
            var u = 1F - t;
            return new PointF(
                u * u * u * p0.X + 3F * u * u * t * p1.X + 3F * u * t * t * p2.X + t * t * t * p3.X,
                u * u * u * p0.Y + 3F * u * u * t * p1.Y + 3F * u * t * t * p2.Y + t * t * t * p3.Y);
        }

        private static GraphicsPath Rounded(RectangleF bounds, float radius)
        {
            var path = new GraphicsPath();
            var diameter = Math.Min(Math.Min(bounds.Width, bounds.Height), radius * 2F);
            path.AddArc(bounds.Left, bounds.Top, diameter, diameter, 180, 90);
            path.AddArc(bounds.Right - diameter, bounds.Top, diameter, diameter, 270, 90);
            path.AddArc(bounds.Right - diameter, bounds.Bottom - diameter, diameter, diameter, 0, 90);
            path.AddArc(bounds.Left, bounds.Bottom - diameter, diameter, diameter, 90, 90);
            path.CloseFigure();
            return path;
        }
    }
}
