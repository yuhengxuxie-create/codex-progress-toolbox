namespace TreasureChest.UI;

/// <summary>
/// A single immutable-pixel compositor for the sidebar transition. Endpoint
/// surfaces are captured once; intermediate paints never relayout the real
/// navigation/content tree.
/// </summary>
internal sealed class SidebarTransitionOverlay : Control, IDpiLayoutExcluded, IExplicitAnimationPaintSource
{
    private Bitmap? _expandedFrame;
    private Bitmap? _collapsedFrame;
    private Bitmap? _navigationMotionFrame;
    private GdiSurface? _expandedSurface;
    private GdiSurface? _collapsedSurface;
    private GdiSurface? _navigationMotionSurface;
    private Bitmap? _primedExpandedFrame;
    private Bitmap? _primedCollapsedFrame;
    private Bitmap? _primedNavigationMotionFrame;
    private SidebarFrameGeometry _expanded;
    private SidebarFrameGeometry _collapsed;
    private SidebarFrameGeometry _primedExpandedGeometry;
    private SidebarFrameGeometry _primedCollapsedGeometry;
    private Size _primedSize;
    private long _primedPaletteFrameId;
    private Rectangle _toggleBounds;
    private Color _background;
    private double _progress;

    public SidebarTransitionOverlay()
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint | ControlStyles.Opaque, true);
        TabStop = false;
        AccessibleRole = AccessibleRole.None;
        Cursor = Cursors.Default;
        Visible = false;
    }

    public event EventHandler? ToggleRequested;
    internal event EventHandler? ProgressChanged;

    public double Progress => _progress;
    public long SnapshotId { get; private set; }
    public long PaletteFrameId { get; private set; }
    public int EndpointCaptureCount { get; private set; }
    public double LastSynchronousPaintMilliseconds { get; private set; }
    public double MaximumSynchronousPaintMilliseconds { get; private set; }
    internal int CompletedPaintCount { get; private set; }
    internal bool HasPrimedFrameForTests => _primedExpandedFrame is not null &&
        _primedCollapsedFrame is not null && _primedNavigationMotionFrame is not null;

    public void Prime(
        Control shell,
        Bitmap expandedFrame,
        Bitmap collapsedFrame,
        Bitmap navigationMotionFrame,
        SidebarFrameGeometry expandedGeometry,
        SidebarFrameGeometry collapsedGeometry,
        long paletteFrameId)
    {
        ArgumentNullException.ThrowIfNull(shell);
        ArgumentNullException.ThrowIfNull(expandedFrame);
        ArgumentNullException.ThrowIfNull(collapsedFrame);
        ArgumentNullException.ThrowIfNull(navigationMotionFrame);
        if (Visible || shell.ClientSize.Width <= 0 || shell.ClientSize.Height <= 0)
        {
            expandedFrame.Dispose();
            collapsedFrame.Dispose();
            navigationMotionFrame.Dispose();
            return;
        }
        Bounds = shell.Bounds;
        if (!IsHandleCreated && Parent is { IsHandleCreated: true }) _ = Handle;
        _primedExpandedFrame?.Dispose();
        _primedCollapsedFrame?.Dispose();
        _primedNavigationMotionFrame?.Dispose();
        _primedExpandedFrame = expandedFrame;
        _primedCollapsedFrame = collapsedFrame;
        _primedNavigationMotionFrame = navigationMotionFrame;
        _primedExpandedGeometry = expandedGeometry;
        _primedCollapsedGeometry = collapsedGeometry;
        _primedSize = shell.ClientSize;
        _primedPaletteFrameId = paletteFrameId;
    }

    internal bool HasPrimedEndpoints(Size size, long paletteFrameId) =>
        _primedExpandedFrame is not null && _primedCollapsedFrame is not null &&
        _primedNavigationMotionFrame is not null &&
        _primedSize == size && _primedPaletteFrameId == paletteFrameId;

    public void Begin(
        Control shell,
        Rectangle bounds,
        SidebarFrameGeometry sourceGeometry,
        double progress,
        Color background,
        long paletteFrameId)
    {
        ArgumentNullException.ThrowIfNull(shell);
        Bounds = bounds;
        _progress = Math.Clamp(progress, 0D, 1D);
        _background = background;
        PaletteFrameId = paletteFrameId;
        DisposeEndpoints();
        if (HasPrimedEndpoints(shell.ClientSize, paletteFrameId))
        {
            _expandedFrame = _primedExpandedFrame;
            _collapsedFrame = _primedCollapsedFrame;
            _navigationMotionFrame = _primedNavigationMotionFrame;
            _expanded = _primedExpandedGeometry;
            _collapsed = _primedCollapsedGeometry;
            _primedExpandedFrame = null;
            _primedCollapsedFrame = null;
            _primedNavigationMotionFrame = null;
        }
        else
        {
            _primedExpandedFrame?.Dispose();
            _primedCollapsedFrame?.Dispose();
            _primedNavigationMotionFrame?.Dispose();
            _primedExpandedFrame = null;
            _primedCollapsedFrame = null;
            _primedNavigationMotionFrame = null;
            var current = CaptureVisibleSurfaceSnapshot(shell);
            if (_progress < 0.5D)
            {
                _expandedFrame = current;
                _collapsedFrame = new Bitmap(current);
                _expanded = sourceGeometry;
                _collapsed = sourceGeometry;
            }
            else
            {
                _collapsedFrame = current;
                _expandedFrame = new Bitmap(current);
                _collapsed = sourceGeometry;
                _expanded = sourceGeometry;
            }
            _navigationMotionFrame = new Bitmap(current);
        }
        _expandedSurface = new GdiSurface(_expandedFrame ??
            throw new InvalidOperationException("侧栏展开端点画面缺失。"));
        _collapsedSurface = new GdiSurface(_collapsedFrame ??
            throw new InvalidOperationException("侧栏收起端点画面缺失。"));
        _navigationMotionSurface = new GdiSurface(_navigationMotionFrame ??
            throw new InvalidOperationException("侧栏动画导航画面缺失。"));
        SnapshotId++;
        EndpointCaptureCount = 1;
        if (!Visible) Visible = true;
        // The overlay is intentionally invisible when MainForm creates its
        // child tree, so WinForms does not create this child HWND during the
        // initial Form.Show.  Update/Invalidate on a handle-less child are
        // no-ops and previously made the 280ms timeline complete with zero
        // visible paints.  Materialize the handle only when a transition starts;
        // its sibling order is unchanged, leaving the resize grip above it.
        if (!IsHandleCreated && Parent is { IsHandleCreated: true }) _ = Handle;
        // Controls.Add leaves this initially-invisible child behind the opaque
        // shell on some WinForms z-order paths.  A visible handle behind the
        // shell receives no WM_PAINT, so the timeline appears to jump directly
        // to its endpoint.  Put the compositor above the shell now; MainForm
        // immediately raises the resize grip again after endpoint capture.
        BringToFront();
        Invalidate();
        // Commit the immutable source before MainForm hides the real shell.  One
        // GDI blit is cheap and guarantees there is no blank/native-child frame
        // between the user click and the first compositor pulse.
        Update();
    }

    public void SetEndpointGeometry(bool collapsed, SidebarFrameGeometry geometry)
    {
        if (collapsed)
        {
            _collapsed = geometry;
        }
        else
        {
            _expanded = geometry;
        }
        EndpointCaptureCount++;
    }

    public void SealSource(Rectangle toggleBounds)
    {
        _toggleBounds = toggleBounds;
        Invalidate();
    }

    public void SetProgress(double progress, bool commitSynchronously = true)
    {
        _progress = Math.Clamp(progress, 0D, 1D);
        Invalidate();
        // This is the sole animated surface.  Commit its paint on the current
        // compositor-paced UI tick instead of letting unrelated child-window
        // invalidations decide when the visual frame becomes observable.
        if (commitSynchronously)
        {
            var paintStartedAt = System.Diagnostics.Stopwatch.GetTimestamp();
            Update();
            LastSynchronousPaintMilliseconds = System.Diagnostics.Stopwatch
                .GetElapsedTime(paintStartedAt).TotalMilliseconds;
            MaximumSynchronousPaintMilliseconds = Math.Max(
                MaximumSynchronousPaintMilliseconds, LastSynchronousPaintMilliseconds);
        }
        ProgressChanged?.Invoke(this, EventArgs.Empty);
    }

    public void Finish()
    {
        Visible = false;
        _toggleBounds = Rectangle.Empty;
        PreserveEndpointsForNextTransition();
    }

    protected override void OnPaintBackground(PaintEventArgs e) => e.Graphics.Clear(_background);

    protected override void OnPaint(PaintEventArgs e)
    {
        CompletedPaintCount++;
        // Endpoint surfaces are opaque screenshots.  HighQualityBicubic on two
        // near-window-sized images can monopolize the UI thread for hundreds of
        // milliseconds, defeating the compositor-paced 165Hz frame pump.  The
        // intermediate transform therefore uses the GPU-friendly bitmap path;
        // exact endpoints remain unscaled below.
        e.Graphics.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceCopy;
        e.Graphics.CompositingQuality = System.Drawing.Drawing2D.CompositingQuality.HighSpeed;
        e.Graphics.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.NearestNeighbor;
        e.Graphics.PixelOffsetMode = System.Drawing.Drawing2D.PixelOffsetMode.None;
        e.Graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.None;
        if (_expandedFrame is null || _collapsedFrame is null)
        {
            AnimationRunner.ReportPaint(this);
            return;
        }

        if (_progress <= 0.000001D)
        {
            e.Graphics.DrawImageUnscaled(_expandedFrame, Point.Empty);
            AnimationRunner.ReportPaint(this);
            return;
        }
        if (_progress >= 0.999999D)
        {
            e.Graphics.DrawImageUnscaled(_collapsedFrame, Point.Empty);
            AnimationRunner.ReportPaint(this);
            return;
        }

        var nav = Interpolate(_expanded.Navigation, _collapsed.Navigation, _progress);
        var gap = Interpolate(_expanded.Gap, _collapsed.Gap, _progress);
        var content = Interpolate(_expanded.Content, _collapsed.Content, _progress);
        var navigationEdgeWidth = Math.Max(1,
            (int)Math.Round(_collapsed.Navigation.Width * (28D / 82D)));

        e.Graphics.Clear(_background);
        if (OperatingSystem.IsWindows() && _expandedSurface is not null && _collapsedSurface is not null)
        {
            var destination = e.Graphics.GetHdc();
            try
            {
                // Never alpha-cross-fade or scale two differently laid-out
                // screenshots. Both operations corrupt ClearType glyph masks:
                // the first creates doubles, the second squeezes letters into
                // thin teeth near the collapsed endpoint. The widest immutable
                // endpoint for each region is copied at native pixel size and
                // clipped by the moving geometry instead. Exact endpoints are
                // still emitted by the fast paths above.
                DrawCrispNavigationRegion(destination, nav, _expanded.Navigation,
                    _navigationMotionSurface, navigationEdgeWidth);
                DrawCrispEndpointRegion(destination, content, _collapsed.Content, _collapsedSurface);
            }
            finally
            {
                e.Graphics.ReleaseHdc(destination);
            }
        }
        else
        {
            DrawCrispNavigationRegionFallback(e.Graphics, nav, _expanded.Navigation,
                _navigationMotionFrame, navigationEdgeWidth);
            DrawCrispEndpointRegionFallback(e.Graphics, content, _collapsed.Content, _collapsedFrame);
        }
        using (var gapBrush = new SolidBrush(_background)) e.Graphics.FillRectangle(gapBrush, gap);
        AnimationRunner.ReportPaint(this);
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        base.OnMouseUp(e);
        if (e.Button == MouseButtons.Left && _toggleBounds.Contains(e.Location))
            ToggleRequested?.Invoke(this, EventArgs.Empty);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            DisposeEndpoints();
            _primedExpandedFrame?.Dispose();
            _primedCollapsedFrame?.Dispose();
            _primedNavigationMotionFrame?.Dispose();
        }
        base.Dispose(disposing);
    }

    private void DisposeEndpoints()
    {
        _expandedSurface?.Dispose();
        _collapsedSurface?.Dispose();
        _navigationMotionSurface?.Dispose();
        _expandedSurface = null;
        _collapsedSurface = null;
        _navigationMotionSurface = null;
        _expandedFrame?.Dispose();
        _collapsedFrame?.Dispose();
        _navigationMotionFrame?.Dispose();
        _expandedFrame = null;
        _collapsedFrame = null;
        _navigationMotionFrame = null;
    }

    private void PreserveEndpointsForNextTransition()
    {
        if (_expandedFrame is null || _collapsedFrame is null || _navigationMotionFrame is null) return;

        // The two immutable endpoint bitmaps that just drove the visible
        // transition are still exact for a reversal at the same size/palette.
        // Move them back into the prewarmed slot instead of temporarily
        // relaying out the live control tree after the endpoint.  The previous
        // post-animation prime could be observed by PrintWindow/native child
        // composition as a wide sidebar with collapsed button content.
        _expandedSurface?.Dispose();
        _collapsedSurface?.Dispose();
        _navigationMotionSurface?.Dispose();
        _expandedSurface = null;
        _collapsedSurface = null;
        _navigationMotionSurface = null;
        _primedExpandedFrame?.Dispose();
        _primedCollapsedFrame?.Dispose();
        _primedNavigationMotionFrame?.Dispose();
        _primedExpandedFrame = _expandedFrame;
        _primedCollapsedFrame = _collapsedFrame;
        _primedNavigationMotionFrame = _navigationMotionFrame;
        _expandedFrame = null;
        _collapsedFrame = null;
        _navigationMotionFrame = null;
        _primedExpandedGeometry = _expanded;
        _primedCollapsedGeometry = _collapsed;
        _primedSize = _primedExpandedFrame.Size;
        _primedPaletteFrameId = PaletteFrameId;
    }

    private static void DrawCrispEndpointRegion(
        IntPtr destination,
        Rectangle targetBounds,
        Rectangle sourceBounds,
        GdiSurface? source)
    {
        if (targetBounds.Width <= 0 || targetBounds.Height <= 0 ||
            sourceBounds.Width <= 0 || sourceBounds.Height <= 0 || source is null) return;
        var width = Math.Min(targetBounds.Width, sourceBounds.Width);
        var height = Math.Min(targetBounds.Height, sourceBounds.Height);
        NativeMethods.BitBlt(destination, targetBounds.X, targetBounds.Y, width, height,
            source.DeviceContext, sourceBounds.X, sourceBounds.Y,
            NativeMethods.SourceCopyRasterOperation);
    }

    private static void DrawCrispNavigationRegion(
        IntPtr destination,
        Rectangle targetBounds,
        Rectangle sourceBounds,
        GdiSurface? source,
        int requestedEdgeWidth)
    {
        if (targetBounds.Width <= 0 || targetBounds.Height <= 0 ||
            sourceBounds.Width <= 0 || sourceBounds.Height <= 0 || source is null) return;

        // The animation-only endpoint deliberately contains no labels. Preserve
        // its right rounded edge instead of clipping an expanded rectangle at a
        // moving width: the left body and the native-size edge are stitched
        // together without scaling glyphs or ClearType pixels.
        var width = Math.Min(targetBounds.Width, sourceBounds.Width);
        var height = Math.Min(targetBounds.Height, sourceBounds.Height);
        var edgeWidth = Math.Min(Math.Max(1, requestedEdgeWidth), width / 2);
        var bodyWidth = Math.Max(0, width - edgeWidth);
        if (bodyWidth > 0)
        {
            NativeMethods.BitBlt(destination, targetBounds.X, targetBounds.Y, bodyWidth, height,
                source.DeviceContext, sourceBounds.X, sourceBounds.Y,
                NativeMethods.SourceCopyRasterOperation);
        }
        NativeMethods.BitBlt(destination, targetBounds.Right - edgeWidth, targetBounds.Y, edgeWidth, height,
            source.DeviceContext, sourceBounds.Right - edgeWidth, sourceBounds.Y,
            NativeMethods.SourceCopyRasterOperation);
    }

    private static Rectangle Interpolate(Rectangle from, Rectangle to, double amount) => new(
        (int)Math.Round(from.X + ((to.X - from.X) * amount)),
        (int)Math.Round(from.Y + ((to.Y - from.Y) * amount)),
        (int)Math.Round(from.Width + ((to.Width - from.Width) * amount)),
        (int)Math.Round(from.Height + ((to.Height - from.Height) * amount)));

    private static void DrawCrispEndpointRegionFallback(
        Graphics graphics,
        Rectangle targetBounds,
        Rectangle sourceBounds,
        Bitmap? source)
    {
        if (source is null || targetBounds.Width <= 0 || targetBounds.Height <= 0 ||
            sourceBounds.Width <= 0 || sourceBounds.Height <= 0)
            return;
        var width = Math.Min(targetBounds.Width, sourceBounds.Width);
        var height = Math.Min(targetBounds.Height, sourceBounds.Height);
        var destination = new Rectangle(targetBounds.X, targetBounds.Y, width, height);
        var sourceRectangle = new Rectangle(sourceBounds.X, sourceBounds.Y, width, height);
        graphics.DrawImage(source, destination, sourceRectangle, GraphicsUnit.Pixel);
    }

    private static void DrawCrispNavigationRegionFallback(
        Graphics graphics,
        Rectangle targetBounds,
        Rectangle sourceBounds,
        Bitmap? source,
        int requestedEdgeWidth)
    {
        if (source is null || targetBounds.Width <= 0 || targetBounds.Height <= 0 ||
            sourceBounds.Width <= 0 || sourceBounds.Height <= 0) return;
        var width = Math.Min(targetBounds.Width, sourceBounds.Width);
        var height = Math.Min(targetBounds.Height, sourceBounds.Height);
        var edgeWidth = Math.Min(Math.Max(1, requestedEdgeWidth), width / 2);
        var bodyWidth = Math.Max(0, width - edgeWidth);
        if (bodyWidth > 0)
        {
            graphics.DrawImage(source,
                new Rectangle(targetBounds.X, targetBounds.Y, bodyWidth, height),
                new Rectangle(sourceBounds.X, sourceBounds.Y, bodyWidth, height), GraphicsUnit.Pixel);
        }
        graphics.DrawImage(source,
            new Rectangle(targetBounds.Right - edgeWidth, targetBounds.Y, edgeWidth, height),
            new Rectangle(sourceBounds.Right - edgeWidth, sourceBounds.Y, edgeWidth, height), GraphicsUnit.Pixel);
    }

    internal static Bitmap CaptureWholeSurfaceSnapshot(Control shell)
    {
        var startedAt = System.Diagnostics.Stopwatch.GetTimestamp();
        var bitmap = new Bitmap(Math.Max(1, shell.ClientSize.Width), Math.Max(1, shell.ClientSize.Height));
        using (var graphics = Graphics.FromImage(bitmap)) graphics.Clear(UiTheme.Background);

        // Draw the root exactly once. Earlier code then redrew every container
        // and every descendant recursively; a nested page could therefore be
        // rendered six or more times before the first animation frame. Root
        // DrawToBitmap supplies all container material. Overlay only atomic
        // visual descendants that WinForms may omit from a parent's snapshot.
        PaintControl(shell, shell, bitmap, shell.ClientRectangle);
        CaptureAtomicDescendants(shell, shell, bitmap, shell.ClientRectangle);
        LastWholeSurfaceCaptureMilliseconds = System.Diagnostics.Stopwatch
            .GetElapsedTime(startedAt).TotalMilliseconds;
        return bitmap;
    }

    internal static double LastWholeSurfaceCaptureMilliseconds { get; private set; }

    private static Bitmap CaptureVisibleSurfaceSnapshot(Control shell)
    {
        // Sidebar endpoint layouts are captured while their live tree is held
        // behind WM_SETREDRAW. Printing the top-level HWND in that state omits
        // DataGridView rows and some native editor descendants. Compose the
        // managed tree and its atomic HWND children instead; this path is
        // independent of desktop occlusion and of the suspended paint queue.
        return CaptureWholeSurfaceSnapshot(shell);
    }

    internal static Bitmap CaptureWindowClientSnapshot(Control control)
    {
        ArgumentNullException.ThrowIfNull(control);
        var root = control is Form ? control : control.FindForm() ?? control;
        var rootBitmap = new Bitmap(Math.Max(1, root.ClientSize.Width), Math.Max(1, root.ClientSize.Height),
            System.Drawing.Imaging.PixelFormat.Format32bppPArgb);
        using var graphics = Graphics.FromImage(rootBitmap);
        graphics.Clear(root.BackColor);
        var hdc = graphics.GetHdc();
        var printed = false;
        try
        {
            printed = root.IsHandleCreated && NativeMethods.PrintWindow(root.Handle, hdc,
                NativeMethods.PrintWindowClientOnly | NativeMethods.PrintWindowRenderFullContent);
        }
        finally
        {
            graphics.ReleaseHdc(hdc);
        }

        if (printed)
        {
            if (ReferenceEquals(root, control))
            {
                return rootBitmap;
            }

            var origin = root.PointToClient(control.PointToScreen(Point.Empty));
            var sourceBounds = new Rectangle(origin, control.ClientSize);
            var rootBounds = new Rectangle(Point.Empty, rootBitmap.Size);
            if (rootBounds.Contains(sourceBounds))
            {
                var cropped = new Bitmap(Math.Max(1, sourceBounds.Width), Math.Max(1, sourceBounds.Height),
                    System.Drawing.Imaging.PixelFormat.Format32bppPArgb);
                using var destination = Graphics.FromImage(cropped);
                destination.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceCopy;
                destination.DrawImage(rootBitmap, new Rectangle(Point.Empty, cropped.Size),
                    sourceBounds, GraphicsUnit.Pixel);
                rootBitmap.Dispose();
                return cropped;
            }
        }

        rootBitmap.Dispose();
        return CaptureWholeSurfaceSnapshot(control);
    }

    private static void CaptureAtomicDescendants(
        Control shell,
        Control control,
        Bitmap target,
        Rectangle inheritedClip)
    {
        // WinForms child index zero is front-most. Paint back-to-front so a
        // later front sibling replaces the exact pixels beneath it.
        for (var index = control.Controls.Count - 1; index >= 0; index--)
        {
            var child = control.Controls[index];
            if (!child.Visible || child.ClientSize.Width <= 0 || child.ClientSize.Height <= 0) continue;
            var childBounds = new Rectangle(
                shell.PointToClient(child.PointToScreen(Point.Empty)), child.ClientSize);
            var visibleBounds = Rectangle.Intersect(inheritedClip, childBounds);
            if (visibleBounds.Width <= 0 || visibleBounds.Height <= 0) continue;
            if (IsAtomicVisual(child))
            {
                PaintControl(shell, child, target, visibleBounds);
                continue;
            }
            CaptureAtomicDescendants(shell, child, target, visibleBounds);
        }
    }

    private static bool IsAtomicVisual(Control control) => control.Controls.Count == 0 || control is
        DataGridView or ListBox or ComboBox or TextBoxBase or UpDownBase;

    private static void PaintControl(Control shell, Control control, Bitmap target, Rectangle visibleBounds)
    {
        var origin = ReferenceEquals(control, shell)
            ? Point.Empty
            : shell.PointToClient(control.PointToScreen(Point.Empty));
        using var surface = new Bitmap(control.ClientSize.Width, control.ClientSize.Height);
        using (var background = Graphics.FromImage(surface))
        {
            background.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceCopy;
            background.DrawImage(target, new Rectangle(Point.Empty, surface.Size),
                origin.X, origin.Y, surface.Width, surface.Height, GraphicsUnit.Pixel);
        }
        try
        {
            // The root shell is deliberately captured while WM_SETREDRAW is
            // suspended so its opposite endpoint can be prepared without a
            // visible layout flash.  WM_PRINTCLIENT obeys that suspension on a
            // real, handle-created MainForm tree and consequently returns only
            // fragments of the root material; the later atomic-child pass then
            // produces the grey/white sliced frame seen by real WGC.  WinForms'
            // managed DrawToBitmap path walks the complete logical tree without
            // consuming the suspended window paint queue, which is the contract
            // this method's root-once composition has always intended.
            if (ReferenceEquals(control, shell))
            {
                control.DrawToBitmap(surface, new Rectangle(Point.Empty, surface.Size));
            }
            else if (control.IsHandleCreated)
            {
                PaintClientHandle(control, surface);
            }
            else
            {
                control.DrawToBitmap(surface, new Rectangle(Point.Empty, surface.Size));
            }
        }
        catch (ArgumentException) when (control.IsHandleCreated)
        {
            PaintClientHandle(control, surface);
        }
        catch (System.Runtime.InteropServices.ExternalException) when (control.IsHandleCreated)
        {
            PaintClientHandle(control, surface);
        }
        using var destination = Graphics.FromImage(target);
        var state = destination.Save();
        destination.SetClip(visibleBounds, System.Drawing.Drawing2D.CombineMode.Intersect);
        destination.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceCopy;
        destination.DrawImageUnscaled(surface, origin);
        destination.Restore(state);
    }

    private static void PaintClientHandle(Control control, Bitmap surface)
    {
        const int wmPrintClient = 0x0318;
        const int prfClient = 0x00000004;
        const int prfEraseBackground = 0x00000008;
        const int prfChildren = 0x00000010;
        using var graphics = Graphics.FromImage(surface);
        var hdc = graphics.GetHdc();
        try
        {
            NativeMethods.SendMessage(control.Handle, wmPrintClient, hdc,
                (IntPtr)(prfClient | prfEraseBackground | prfChildren));
        }
        finally
        {
            graphics.ReleaseHdc(hdc);
        }
    }

    internal static Bitmap CaptureWholeSurfaceForTests(Control shell) => CaptureWholeSurfaceSnapshot(shell);

    internal static Bitmap CaptureVisibleSurfaceForTransition(Control shell) =>
        CaptureVisibleSurfaceSnapshot(shell);

    private sealed class GdiSurface : IDisposable
    {
        private readonly IntPtr _bitmap;
        private readonly IntPtr _previous;
        private bool _disposed;

        internal GdiSurface(Bitmap bitmap)
        {
            _bitmap = bitmap.GetHbitmap();
            DeviceContext = NativeMethods.CreateCompatibleDC(IntPtr.Zero);
            if (DeviceContext == IntPtr.Zero)
            {
                NativeMethods.DeleteObject(_bitmap);
                throw new InvalidOperationException("无法创建侧栏合成内存画布。");
            }
            _previous = NativeMethods.SelectObject(DeviceContext, _bitmap);
        }

        internal IntPtr DeviceContext { get; }

        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            if (_previous != IntPtr.Zero) NativeMethods.SelectObject(DeviceContext, _previous);
            NativeMethods.DeleteObject(_bitmap);
            NativeMethods.DeleteDC(DeviceContext);
        }
    }
}

internal readonly record struct SidebarFrameGeometry(
    Rectangle Navigation,
    Rectangle Gap,
    Rectangle Content);
