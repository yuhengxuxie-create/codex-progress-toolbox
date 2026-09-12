using System.Drawing.Imaging;
using System.Runtime.InteropServices;

namespace TreasureChest.UI;

/// <summary>
/// One opaque compositor for a whole themed window.  The real control tree is
/// kept at its source endpoint during intermediate frames; only this surface is
/// painted.  That makes a palette frame atomic and avoids recursively mutating
/// and synchronously repainting every WinForms child at 165 Hz.
/// </summary>
internal sealed class ThemeTransitionOverlay : Control, IDpiLayoutExcluded, IExplicitAnimationPaintSource
{
    // A one logical-pixel pen/rounded edge plus its low-coverage fringe can
    // span four device pixels at the highest supported scale.  Keep this semantic (role
    // proximity) gate rather than mapping every near-palette colour: text
    // and unrelated gradients must continue through their own paths.
    private const int RoleEdgeRadius = 4;

    private Bitmap? _source;
    private Bitmap? _animationSource;
    private Bitmap? _dayEndpoint;
    private Bitmap? _nightEndpoint;
    private Bitmap? _bridgeEndpoint;
    private GdiSurface? _sourceSurface;
    private GdiSurface? _animationSourceSurface;
    private GdiSurface? _daySurface;
    private GdiSurface? _nightSurface;
    private GdiSurface? _bridgeSurface;
    private ThemeMode _targetMode;
    private double _progress;

    internal ThemeTransitionOverlay()
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint | ControlStyles.Opaque, true);
        TabStop = false;
        AccessibleRole = AccessibleRole.None;
        Cursor = Cursors.Default;
        Visible = false;
    }

    internal double Progress => _progress;
    internal int CompletedPaintCount { get; private set; }
    internal double MaximumPaintMilliseconds { get; private set; }
    internal static double LastPaletteMappingMilliseconds { get; private set; }

    internal readonly record struct StableContentRegion(Rectangle Bounds, bool Elliptical)
    {
        internal bool Contains(int x, int y)
        {
            if (!Bounds.Contains(x, y)) return false;
            if (!Elliptical) return true;
            var radiusX = Bounds.Width / 2D;
            var radiusY = Bounds.Height / 2D;
            if (radiusX <= 0D || radiusY <= 0D) return false;
            var normalizedX = (x + 0.5D - (Bounds.Left + radiusX)) / radiusX;
            var normalizedY = (y + 0.5D - (Bounds.Top + radiusY)) / radiusY;
            return normalizedX * normalizedX + normalizedY * normalizedY <= 1D;
        }
    }

    internal static StableContentRegion[] CaptureStableContentRegions(Form form)
    {
        ArgumentNullException.ThrowIfNull(form);
        var regions = new List<StableContentRegion>();
        Visit(form);
        return regions.ToArray();

        void Visit(Control parent)
        {
            foreach (Control child in parent.Controls)
            {
                if (!child.Visible || child.IsDisposed) continue;
                if (child is AppAvatarControl avatar)
                {
                    var localBounds = avatar.StableAnimationContentBounds;
                    if (localBounds.Width > 0 && localBounds.Height > 0)
                    {
                        var screenOrigin = avatar.PointToScreen(localBounds.Location);
                        var clientOrigin = form.PointToClient(screenOrigin);
                        var bounds = Rectangle.Intersect(form.ClientRectangle,
                            new Rectangle(clientOrigin, localBounds.Size));
                        if (bounds.Width > 0 && bounds.Height > 0)
                            regions.Add(new StableContentRegion(bounds, Elliptical: true));
                    }
                }
                Visit(child);
            }
        }
    }

    internal static Bitmap CaptureFormClient(Form form, bool allowScreenCapture = true)
    {
        ArgumentNullException.ThrowIfNull(form);
        if (allowScreenCapture && form.Visible && form.IsHandleCreated &&
            form.WindowState != FormWindowState.Minimized)
        {
            // The endpoint must come from the target HWND, never from desktop
            // pixels. Desktop capture baked occluders, pointer feedback and stale
            // native child surfaces into the immutable animation snapshot.
            return SidebarTransitionOverlay.CaptureWindowClientSnapshot(form);
        }
        return SidebarTransitionOverlay.CaptureWholeSurfaceSnapshot(form);
    }

    /// <summary>
    /// Owns both the managed endpoint and its native memory surface.  Creating
    /// the HBITMAP/DC pair is deliberately done by the idle prewarm path, not by
    /// the user's click callback.
    /// </summary>
    internal sealed class PreparedFrame : IDisposable
    {
        private Bitmap? _bitmap;
        private GdiSurface? _surface;

        internal PreparedFrame(Bitmap bitmap)
        {
            _bitmap = bitmap;
            try { _surface = new GdiSurface(bitmap); }
            catch
            {
                _bitmap.Dispose();
                _bitmap = null;
                throw;
            }
        }

        internal (Bitmap Bitmap, GdiSurface Surface) Take()
        {
            if (_bitmap is null || _surface is null)
                throw new InvalidOperationException("主题预热画面已经被消费。");
            var result = (_bitmap, _surface);
            _bitmap = null;
            _surface = null;
            return result;
        }

        public void Dispose()
        {
            _surface?.Dispose();
            _bitmap?.Dispose();
            _surface = null;
            _bitmap = null;
        }
    }

    internal void BeginSource(
        Form form,
        Bitmap source,
        ThemeMode sourceMode,
        Rectangle excludedBounds)
    {
        ArgumentNullException.ThrowIfNull(form);
        ArgumentNullException.ThrowIfNull(source);
        Bounds = form.ClientRectangle;
        _source = source;
        _sourceSurface = new GdiSurface(_source);
        if (sourceMode == ThemeMode.Day)
        {
            _dayEndpoint = _source;
            _daySurface = _sourceSurface;
        }
        else
        {
            _nightEndpoint = _source;
            _nightSurface = _sourceSurface;
        }
        _targetMode = sourceMode;
        _progress = 0D;
        BackColor = UiTheme.Background;

        SetExcludedBounds(excludedBounds);

        if (!Visible) Visible = true;
        if (!IsHandleCreated && Parent is { IsHandleCreated: true }) _ = Handle;
        BringToFront();
        Invalidate();
        Update();
    }

    internal void BeginPrepared(
        Form form,
        PreparedFrame source,
        PreparedFrame animationSource,
        ThemeMode sourceMode,
        PreparedFrame bridge,
        PreparedFrame target,
        ThemeMode targetMode,
        Rectangle excludedBounds)
    {
        ArgumentNullException.ThrowIfNull(form);
        ArgumentNullException.ThrowIfNull(source);
        ArgumentNullException.ThrowIfNull(animationSource);
        ArgumentNullException.ThrowIfNull(bridge);
        ArgumentNullException.ThrowIfNull(target);
        (_source, _sourceSurface) = source.Take();
        (_animationSource, _animationSourceSurface) = animationSource.Take();
        (_bridgeEndpoint, _bridgeSurface) = bridge.Take();
        var (targetBitmap, targetSurface) = target.Take();
        if (sourceMode == ThemeMode.Day)
        {
            _dayEndpoint = _animationSource;
            _daySurface = _animationSourceSurface;
            _nightEndpoint = targetBitmap;
            _nightSurface = targetSurface;
        }
        else
        {
            _nightEndpoint = _animationSource;
            _nightSurface = _animationSourceSurface;
            _dayEndpoint = targetBitmap;
            _daySurface = targetSurface;
        }
        _targetMode = targetMode;
        _progress = 0D;
        Bounds = form.ClientRectangle;
        BackColor = UiTheme.Background;
        SetExcludedBounds(excludedBounds);
        if (!Visible) Visible = true;
        if (!IsHandleCreated && Parent is { IsHandleCreated: true }) _ = Handle;
        BringToFront();
        Invalidate();
        Update();
    }

    internal void SetExcludedBounds(Rectangle excludedBounds)
    {
        var region = new Region(ClientRectangle);
        if (excludedBounds.Width > 0 && excludedBounds.Height > 0)
            region.Exclude(Rectangle.Intersect(ClientRectangle, excludedBounds));
        var previousRegion = Region;
        Region = region;
        previousRegion?.Dispose();
        Invalidate();
    }

    internal void SetAnimationEndpoints(
        Bitmap animationSource,
        ThemeMode sourceMode,
        Bitmap bridgeEndpoint)
    {
        ArgumentNullException.ThrowIfNull(animationSource);
        ArgumentNullException.ThrowIfNull(bridgeEndpoint);
        if (_animationSource is not null && !ReferenceEquals(_animationSource, _source))
            _animationSource.Dispose();
        if (_animationSourceSurface is not null && !ReferenceEquals(_animationSourceSurface, _sourceSurface))
            _animationSourceSurface.Dispose();
        _bridgeEndpoint?.Dispose();
        _bridgeSurface?.Dispose();

        _animationSource = animationSource;
        _animationSourceSurface = new GdiSurface(_animationSource);
        _bridgeEndpoint = bridgeEndpoint;
        _bridgeSurface = new GdiSurface(_bridgeEndpoint);
        if (sourceMode == ThemeMode.Day)
        {
            if (!ReferenceEquals(_dayEndpoint, _source)) _dayEndpoint?.Dispose();
            if (!ReferenceEquals(_daySurface, _sourceSurface)) _daySurface?.Dispose();
            _dayEndpoint = _animationSource;
            _daySurface = _animationSourceSurface;
        }
        else
        {
            if (!ReferenceEquals(_nightEndpoint, _source)) _nightEndpoint?.Dispose();
            if (!ReferenceEquals(_nightSurface, _sourceSurface)) _nightSurface?.Dispose();
            _nightEndpoint = _animationSource;
            _nightSurface = _animationSourceSurface;
        }
    }

    internal void SetTargetEndpoint(Bitmap targetEndpoint, ThemeMode targetMode)
    {
        ArgumentNullException.ThrowIfNull(targetEndpoint);
        if (targetMode == ThemeMode.Night)
        {
            if (!ReferenceEquals(_nightSurface, _sourceSurface)) _nightSurface?.Dispose();
            if (!ReferenceEquals(_nightEndpoint, _source)) _nightEndpoint?.Dispose();
            _nightEndpoint = targetEndpoint;
            _nightSurface = new GdiSurface(_nightEndpoint);
        }
        else
        {
            if (!ReferenceEquals(_daySurface, _sourceSurface)) _daySurface?.Dispose();
            if (!ReferenceEquals(_dayEndpoint, _source)) _dayEndpoint?.Dispose();
            _dayEndpoint = targetEndpoint;
            _daySurface = new GdiSurface(_dayEndpoint);
        }
        _targetMode = targetMode;
        _progress = 0D;
    }

    internal void BuildMappedTargetEndpoint(
        ThemePalette from,
        ThemePalette target,
        ThemeMode targetMode,
        IReadOnlyList<StableContentRegion>? stableContentRegions = null)
    {
        if (_source is null) throw new InvalidOperationException("主题合成源画面尚未建立。");
        Bitmap? animationSource = null;
        Bitmap? bridge = null;
        Bitmap? endpoint = null;
        try
        {
            animationSource = MapPaletteRolesForAnimation(
                _source, from, from, normalizeTextSubpixelCoverage: true,
                stableContentRegions: stableContentRegions);
            bridge = MapPaletteRolesForAnimation(
                _source, from, ThemePalette.AnimationBridge, normalizeTextSubpixelCoverage: true,
                stableContentRegions: stableContentRegions);
            endpoint = MapPaletteRolesForAnimation(
                _source, from, target, normalizeTextSubpixelCoverage: true,
                stableContentRegions: stableContentRegions);
            SetAnimationEndpoints(animationSource, _targetMode, bridge);
            animationSource = null;
            bridge = null;
            SetTargetEndpoint(endpoint, targetMode);
            endpoint = null;
        }
        finally
        {
            animationSource?.Dispose();
            bridge?.Dispose();
            endpoint?.Dispose();
        }
    }

    internal void Retarget(ThemeMode targetMode)
    {
        using var current = RenderCurrentFrame();
        _sourceSurface?.Dispose();
        _source?.Dispose();
        _source = new Bitmap(current);
        _sourceSurface = new GdiSurface(_source);
        if (!ReferenceEquals(_animationSourceSurface, _daySurface) &&
            !ReferenceEquals(_animationSourceSurface, _nightSurface))
            _animationSourceSurface?.Dispose();
        if (!ReferenceEquals(_animationSource, _dayEndpoint) &&
            !ReferenceEquals(_animationSource, _nightEndpoint))
            _animationSource?.Dispose();
        _animationSource = new Bitmap(current);
        _animationSourceSurface = new GdiSurface(_animationSource);
        _targetMode = targetMode;
        _progress = 0D;
        Invalidate();
        Update();
    }

    internal void SetProgress(double progress)
    {
        _progress = Math.Clamp(progress, 0D, 1D);
        var startedAt = System.Diagnostics.Stopwatch.GetTimestamp();
        Invalidate();
        Update();
        MaximumPaintMilliseconds = Math.Max(MaximumPaintMilliseconds,
            System.Diagnostics.Stopwatch.GetElapsedTime(startedAt).TotalMilliseconds);
    }

    internal void Finish()
    {
        Visible = false;
        var previousRegion = Region;
        Region = null;
        previousRegion?.Dispose();
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        // OnPaint always emits a complete opaque frame.
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        CompletedPaintCount++;
        DrawFrame(e.Graphics);
        AnimationRunner.ReportPaint(this);
        UiTheme.ReportThemePaint();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            var disposed = new List<IDisposable>();
            foreach (var item in new IDisposable?[]
                     {
                         _sourceSurface, _animationSourceSurface, _bridgeSurface, _daySurface, _nightSurface,
                         _source, _animationSource, _bridgeEndpoint, _dayEndpoint, _nightEndpoint, Region,
                     })
            {
                if (item is null || disposed.Any(existing => ReferenceEquals(existing, item))) continue;
                item.Dispose();
                disposed.Add(item);
            }
        }
        base.Dispose(disposing);
    }

    private Bitmap RenderCurrentFrame()
    {
        var bitmap = new Bitmap(Math.Max(1, Width), Math.Max(1, Height));
        using var graphics = Graphics.FromImage(bitmap);
        DrawFrame(graphics);
        return bitmap;
    }

    internal Bitmap CaptureCurrentFrameForTests() => RenderCurrentFrame();

    internal static Bitmap MapPaletteRolesForAnimation(
        Bitmap source,
        ThemePalette from,
        ThemePalette target,
        bool normalizeTextSubpixelCoverage = false,
        IReadOnlyList<StableContentRegion>? stableContentRegions = null)
    {
        ArgumentNullException.ThrowIfNull(source);
        var startedAt = System.Diagnostics.Stopwatch.GetTimestamp();
        var width = source.Width;
        var height = source.Height;
        var endpoint = new Bitmap(width, height, PixelFormat.Format32bppArgb);
        using (var graphics = Graphics.FromImage(endpoint)) graphics.DrawImageUnscaled(source, Point.Empty);
        var sourceRoles = from.Colors.ToArray();
        var targetRoles = target.Colors.ToArray();
        var textTransitions = TextSurfaceTransitions(from, target);
        var onAccentTransitions = OnAccentTransitions(from, target);
        var semanticEdgeTransitions = SemanticEdgeTransitions(from, target);
        var textForegroundNeighborhood = NearbyColors(textTransitions.Select(pair => pair.SourceForeground));
        var textBackgroundNeighborhood = NearbyColors(textTransitions.Select(pair => pair.SourceBackground));
        var exactRoleMap = new Dictionary<int, Color>();
        for (var role = 0; role < sourceRoles.Length; role++)
            exactRoleMap.TryAdd(sourceRoles[role].ToArgb(), targetRoles[role]);
        var bounds = new Rectangle(0, 0, width, height);
        var data = endpoint.LockBits(bounds, ImageLockMode.ReadWrite, PixelFormat.Format32bppArgb);
        var bytes = new byte[Math.Abs(data.Stride) * data.Height];
        var completed = false;
        try
        {
            Marshal.Copy(data.Scan0, bytes, 0, bytes.Length);
            var original = (byte[])bytes.Clone();
            Parallel.For(0, height, y =>
            {
                Span<int> nearest = stackalloc int[8];
                Span<int> distances = stackalloc int[8];
                for (var x = 0; x < width; x++)
                {
                    if (IsStableContentPixel(stableContentRegions, x, y))
                        continue;
                    var offset = y * data.Stride + x * 4;
                    var pixel = Color.FromArgb(original[offset + 2], original[offset + 1], original[offset]);
                    if (exactRoleMap.TryGetValue(pixel.ToArgb(), out var exactTarget))
                    {
                        WriteColor(bytes, offset, exactTarget);
                        continue;
                    }
                    var channelSpread = Math.Max(pixel.R, Math.Max(pixel.G, pixel.B)) -
                                        Math.Min(pixel.R, Math.Min(pixel.G, pixel.B));
                    // White product text and checked glyphs over Accent need a
                    // narrow, high-confidence first pass.  Their blue channel is
                    // only 11 levels apart in the day palette, and near-solid
                    // pixels can otherwise fall through to an unrelated role pair
                    // and become green/yellow in the mapped night endpoint.
                    if (TryMapTextSubpixel(original, data.Stride, width, height, x, y,
                            pixel, onAccentTransitions, normalizeTextSubpixelCoverage,
                            contextRadius: RoleEdgeRadius, minimumContextRank: 2,
                            minimumChannelSpan: 4,
                            contextColorTolerance: 4,
                            preferFitOverContext: false,
                            out var textTarget))
                    {
                        WriteColor(bytes, offset, textTarget);
                        continue;
                    }
                    // Rounded outlines, checkbox chrome and numeric arrows are
                    // alpha blends between two semantic palette roles.  Map
                    // those pairs before the broader text heuristic so a pale
                    // day edge cannot survive as a dotted white source fringe.
                    if (TryMapTextSubpixel(original, data.Stride, width, height, x, y,
                            pixel, semanticEdgeTransitions, normalizeSubpixelCoverage: true,
                            contextRadius: RoleEdgeRadius, minimumContextRank: 1,
                            minimumChannelSpan: 12,
                            contextColorTolerance: 18,
                            preferFitOverContext: true,
                            out var semanticEdgeTarget))
                    {
                        WriteColor(bytes, offset, semanticEdgeTarget);
                        continue;
                    }
                    if (channelSpread >= 5 &&
                        TouchesTextContext(original, data.Stride, width, height, x, y,
                            textForegroundNeighborhood, textBackgroundNeighborhood) &&
                        TryMapTextSubpixel(original, data.Stride, width, height, x, y,
                            pixel, textTransitions, normalizeTextSubpixelCoverage,
                            contextRadius: 2, minimumContextRank: 1,
                            minimumChannelSpan: 12, contextColorTolerance: 18,
                            preferFitOverContext: false,
                            out textTarget))
                    {
                        WriteColor(bytes, offset, textTarget);
                        continue;
                    }
                    nearest.Fill(-1);
                    distances.Fill(int.MaxValue);
                    for (var role = 0; role < sourceRoles.Length; role++)
                    {
                        var distance = SquaredDistance(pixel, sourceRoles[role]);
                        for (var slot = 0; slot < nearest.Length; slot++)
                        {
                            if (distance >= distances[slot]) continue;
                            for (var shift = nearest.Length - 1; shift > slot; shift--)
                            {
                                nearest[shift] = nearest[shift - 1];
                                distances[shift] = distances[shift - 1];
                            }
                            nearest[slot] = role;
                            distances[slot] = distance;
                            break;
                        }
                    }
                    if (distances[0] <= 12)
                    {
                        WriteColor(bytes, offset, targetRoles[nearest[0]]);
                        continue;
                    }

                    var bestError = double.MaxValue;
                    var bestLeft = -1;
                    var bestRight = -1;
                    var bestAmount = 0D;
                    for (var leftSlot = 0; leftSlot < nearest.Length - 1; leftSlot++)
                    for (var rightSlot = leftSlot + 1; rightSlot < nearest.Length; rightSlot++)
                    {
                        var left = nearest[leftSlot];
                        var right = nearest[rightSlot];
                        if (left < 0 || right < 0) continue;
                        var a = sourceRoles[left];
                        var b = sourceRoles[right];
                        if (!TouchesRole(original, data.Stride, width, height, x, y, a, b)) continue;
                        var dr = b.R - a.R;
                        var dg = b.G - a.G;
                        var db = b.B - a.B;
                        var length = dr * dr + dg * dg + db * db;
                        if (length < 256) continue;
                        var amount = Math.Clamp(((pixel.R - a.R) * dr + (pixel.G - a.G) * dg +
                                                 (pixel.B - a.B) * db) / (double)length, 0D, 1D);
                        var rr = a.R + dr * amount;
                        var gg = a.G + dg * amount;
                        var bb = a.B + db * amount;
                        var error = (pixel.R - rr) * (pixel.R - rr) +
                                    (pixel.G - gg) * (pixel.G - gg) +
                                    (pixel.B - bb) * (pixel.B - bb);
                        if (error >= bestError) continue;
                        bestError = error;
                        bestLeft = left;
                        bestRight = right;
                        bestAmount = amount;
                    }
                    if (bestError > 90D || bestLeft < 0 ||
                        !TouchesRole(original, data.Stride, width, height, x, y,
                            sourceRoles[bestLeft], sourceRoles[bestRight])) continue;
                    WriteColor(bytes, offset, Interpolate(targetRoles[bestLeft], targetRoles[bestRight], bestAmount));
                }
            });
            Marshal.Copy(bytes, 0, data.Scan0, bytes.Length);
            LastPaletteMappingMilliseconds = System.Diagnostics.Stopwatch
                .GetElapsedTime(startedAt).TotalMilliseconds;
            completed = true;
            return endpoint;
        }
        finally
        {
            endpoint.UnlockBits(data);
            if (!completed) endpoint.Dispose();
        }

        static int SquaredDistance(Color left, Color right)
        {
            var r = left.R - right.R;
            var g = left.G - right.G;
            var b = left.B - right.B;
            return r * r + g * g + b * b;
        }

        static bool IsStableContentPixel(
            IReadOnlyList<StableContentRegion>? regions,
            int x,
            int y)
        {
            if (regions is null) return false;
            for (var index = 0; index < regions.Count; index++)
                if (regions[index].Contains(x, y)) return true;
            return false;
        }

        static bool TouchesRole(byte[] sourceBytes, int stride, int imageWidth, int imageHeight,
            int x, int y, Color left, Color right)
        {
            for (var dy = -RoleEdgeRadius; dy <= RoleEdgeRadius; dy++)
            for (var dx = -RoleEdgeRadius; dx <= RoleEdgeRadius; dx++)
            {
                if (dx == 0 && dy == 0) continue;
                var nx = x + dx;
                var ny = y + dy;
                if (nx < 0 || nx >= imageWidth || ny < 0 || ny >= imageHeight) continue;
                var offset = ny * stride + nx * 4;
                var neighbor = Color.FromArgb(sourceBytes[offset + 2], sourceBytes[offset + 1], sourceBytes[offset]);
                if (SquaredDistance(neighbor, left) <= 48 || SquaredDistance(neighbor, right) <= 48) return true;
            }
            return false;
        }

        static Color Interpolate(Color left, Color right, double amount) => Color.FromArgb(
            (int)Math.Round(left.R + (right.R - left.R) * amount),
            (int)Math.Round(left.G + (right.G - left.G) * amount),
            (int)Math.Round(left.B + (right.B - left.B) * amount));

        static void WriteColor(byte[] targetBytes, int offset, Color color)
        {
            targetBytes[offset] = color.B;
            targetBytes[offset + 1] = color.G;
            targetBytes[offset + 2] = color.R;
            targetBytes[offset + 3] = byte.MaxValue;
        }

        static HashSet<int> NearbyColors(IEnumerable<Color> colors)
        {
            var result = new HashSet<int>();
            foreach (var color in colors.DistinctBy(value => value.ToArgb()))
            for (var redDelta = -5; redDelta <= 5; redDelta++)
            for (var greenDelta = -5; greenDelta <= 5; greenDelta++)
            for (var blueDelta = -5; blueDelta <= 5; blueDelta++)
            {
                var red = color.R + redDelta;
                var green = color.G + greenDelta;
                var blue = color.B + blueDelta;
                if (red is < 0 or > 255 || green is < 0 or > 255 || blue is < 0 or > 255) continue;
                result.Add(Color.FromArgb(red, green, blue).ToArgb());
            }
            return result;
        }

        static bool TouchesTextContext(byte[] bytes, int rowStride, int width, int height,
            int centerX, int centerY, IReadOnlySet<int> foregrounds, IReadOnlySet<int> backgrounds)
        {
            var foregroundFound = false;
            var backgroundFound = false;
            for (var dy = -2; dy <= 2; dy++)
            for (var dx = -2; dx <= 2; dx++)
            {
                if (dx == 0 && dy == 0) continue;
                var nx = centerX + dx;
                var ny = centerY + dy;
                if (nx < 0 || nx >= width || ny < 0 || ny >= height) continue;
                var neighborOffset = ny * rowStride + nx * 4;
                var argb = Color.FromArgb(bytes[neighborOffset + 2], bytes[neighborOffset + 1],
                    bytes[neighborOffset]).ToArgb();
                foregroundFound |= foregrounds.Contains(argb);
                backgroundFound |= backgrounds.Contains(argb);
                if (foregroundFound || backgroundFound) return true;
            }
            return false;
        }

    }

    private static TextSurfaceTransition[] TextSurfaceTransitions(ThemePalette source, ThemePalette target) =>
    [
        new(source.Text, source.Background, target.Text, target.Background),
        new(source.Text, source.Surface, target.Text, target.Surface),
        new(source.Text, source.SurfaceAlt, target.Text, target.SurfaceAlt),
        new(source.Text, source.SurfaceHover, target.Text, target.SurfaceHover),
        new(source.Muted, source.Background, target.Muted, target.Background),
        new(source.Muted, source.Surface, target.Muted, target.Surface),
        new(source.Muted, source.SurfaceAlt, target.Muted, target.SurfaceAlt),
        new(source.NavigationText, source.Navigation, target.NavigationText, target.Navigation),
        new(source.NavigationText, source.NavigationHover, target.NavigationText, target.NavigationHover),
        new(source.NavigationText, source.NavigationPressed, target.NavigationText, target.NavigationPressed),
        new(source.NavigationMuted, source.Navigation, target.NavigationMuted, target.Navigation),
        new(source.NavigationMuted, source.NavigationHover, target.NavigationMuted, target.NavigationHover),
        new(source.NavigationMuted, source.NavigationPressed, target.NavigationMuted, target.NavigationPressed),
        new(source.OnAccent, source.Accent, target.OnAccent, target.Accent),
        new(source.OnAccent, source.AccentHover, target.OnAccent, target.AccentHover),
        new(source.OnAccent, source.AccentPressed, target.OnAccent, target.AccentPressed),
        new(source.DisabledText, source.DisabledBackground, target.DisabledText, target.DisabledBackground),
        new(source.DisabledText, source.Surface, target.DisabledText, target.Surface),
        new(source.Running, source.Surface, target.Running, target.Surface),
        new(source.Running, source.DrawerSelected, target.Running, target.DrawerSelected),
        new(source.Stopped, source.Surface, target.Stopped, target.Surface),
        new(source.Stopped, source.DrawerSelected, target.Stopped, target.DrawerSelected),
        new(source.Warning, source.Surface, target.Warning, target.Surface),
        new(source.Warning, source.DrawerSelected, target.Warning, target.DrawerSelected),
    ];

    private static TextSurfaceTransition[] OnAccentTransitions(ThemePalette source, ThemePalette target) =>
    [
        new(source.OnAccent, source.Accent, target.OnAccent, target.Accent),
        new(source.OnAccent, source.AccentHover, target.OnAccent, target.AccentHover),
        new(source.OnAccent, source.AccentPressed, target.OnAccent, target.AccentPressed),
    ];

    private static TextSurfaceTransition[] SemanticEdgeTransitions(ThemePalette source, ThemePalette target) =>
    [
        // Selected navigation items use NavigationPressed with an Accent
        // outline. Preserve this low-coverage pair before generic edge roles;
        // its blue span is small but still carries the semantic edge mask.
        new(source.NavigationPressed, source.Accent,
            target.NavigationPressed, target.Accent),
        new(source.AccentSoftStrong, source.NavigationPressed,
            target.AccentSoftStrong, target.NavigationPressed),
        new(source.Accent, source.Surface, target.Accent, target.Surface),
        new(source.Border, source.Surface, target.Border, target.Surface),
        new(source.Muted, source.Surface, target.Muted, target.Surface),
    ];

    /// <summary>
    /// ClearType coverage is channel-specific.  Averaging those three coverages
    /// into a grey edge changes the glyph mask and creates the white/black teeth
    /// seen in real intermediate frames.  Preserve each channel's exact coverage
    /// and only substitute the semantic foreground/background pair.  Source and
    /// target then share one pixel-identical glyph mask, so AlphaBlend changes
    /// colour without cross-fading two different text rasters.
    /// </summary>
    private static bool TryMapTextSubpixel(
        byte[] source,
        int stride,
        int imageWidth,
        int imageHeight,
        int x,
        int y,
        Color pixel,
        IReadOnlyList<TextSurfaceTransition> transitions,
        bool normalizeSubpixelCoverage,
        int contextRadius,
        int minimumContextRank,
        int minimumChannelSpan,
        int contextColorTolerance,
        bool preferFitOverContext,
        out Color target)
    {
        var bestScore = double.MaxValue;
        var bestContextRank = 0;
        TextSurfaceTransition best = default;
        var bestRedCoverage = 0D;
        var bestGreenCoverage = 0D;
        var bestBlueCoverage = 0D;
        foreach (var transition in transitions)
        {
            if (!TryCoverage(pixel.R, transition.SourceForeground.R, transition.SourceBackground.R,
                    minimumChannelSpan, out var redCoverage, out var redInformative) ||
                !TryCoverage(pixel.G, transition.SourceForeground.G, transition.SourceBackground.G,
                    minimumChannelSpan, out var greenCoverage, out var greenInformative) ||
                !TryCoverage(pixel.B, transition.SourceForeground.B, transition.SourceBackground.B,
                    minimumChannelSpan, out var blueCoverage, out var blueInformative))
                continue;
            var informativeCount = (redInformative ? 1 : 0) + (greenInformative ? 1 : 0) +
                                   (blueInformative ? 1 : 0);
            if (informativeCount == 0) continue;
            var average = ((redInformative ? redCoverage : 0D) +
                           (greenInformative ? greenCoverage : 0D) +
                           (blueInformative ? blueCoverage : 0D)) / informativeCount;
            if (average is <= 0.01D or >= 0.99D) continue;
            var contextRank = TextContextRank(source, stride, imageWidth, imageHeight, x, y,
                transition.SourceForeground, transition.SourceBackground, contextRadius,
                contextColorTolerance);
            if (contextRank < minimumContextRank) continue;
            var score = Math.Abs(pixel.R - Mix(transition.SourceBackground.R, transition.SourceForeground.R, average)) +
                        Math.Abs(pixel.G - Mix(transition.SourceBackground.G, transition.SourceForeground.G, average)) +
                        Math.Abs(pixel.B - Mix(transition.SourceBackground.B, transition.SourceForeground.B, average));
            if (preferFitOverContext)
            {
                if (score >= bestScore) continue;
            }
            else if (contextRank < bestContextRank ||
                     contextRank == bestContextRank && score >= bestScore)
            {
                continue;
            }
            bestContextRank = contextRank;
            bestScore = score;
            best = transition;
            bestRedCoverage = redCoverage;
            bestGreenCoverage = greenCoverage;
            bestBlueCoverage = blueCoverage;
        }

        if (bestScore == double.MaxValue)
        {
            target = Color.Empty;
            return false;
        }
        if (normalizeSubpixelCoverage)
        {
            var informative = new[]
            {
                (Coverage: bestRedCoverage,
                    IsInformative: Math.Abs(best.SourceForeground.R - best.SourceBackground.R) >= minimumChannelSpan),
                (Coverage: bestGreenCoverage,
                    IsInformative: Math.Abs(best.SourceForeground.G - best.SourceBackground.G) >= minimumChannelSpan),
                (Coverage: bestBlueCoverage,
                    IsInformative: Math.Abs(best.SourceForeground.B - best.SourceBackground.B) >= minimumChannelSpan),
            };
            var neutralCoverage = informative.Where(channel => channel.IsInformative)
                .Average(channel => channel.Coverage);
            bestRedCoverage = neutralCoverage;
            bestGreenCoverage = neutralCoverage;
            bestBlueCoverage = neutralCoverage;
        }
        target = Color.FromArgb(
            Mix(best.TargetBackground.R, best.TargetForeground.R, bestRedCoverage),
            Mix(best.TargetBackground.G, best.TargetForeground.G, bestGreenCoverage),
            Mix(best.TargetBackground.B, best.TargetForeground.B, bestBlueCoverage));
        return true;

        static bool TryCoverage(byte value, byte foreground, byte background,
            int minimumSpan, out double coverage, out bool informative)
        {
            var span = foreground - background;
            // A channel difference of 5-11 is still meaningful for high-chroma
            // pairs such as OnAccent over the day Accent (blue 255 vs 244).
            // Treating that span as absent rejected the correct text role and
            // allowed an unrelated hover/semantic pair to paint green strokes.
            // Only truly indistinguishable channels use the no-information path.
            if (Math.Abs(span) < minimumSpan)
            {
                informative = false;
                coverage = 0D;
                return Math.Abs(value - background) <= 4;
            }
            informative = true;
            coverage = (value - background) / (double)span;
            if (coverage is < -0.025D or > 1.025D) return false;
            coverage = Math.Clamp(coverage, 0D, 1D);
            return true;
        }

        static byte Mix(byte background, byte foreground, double coverage) =>
            (byte)Math.Clamp((int)Math.Round(background + (foreground - background) * coverage), 0, 255);

        static int TextContextRank(byte[] bytes, int rowStride, int width, int height,
            int centerX, int centerY, Color foreground, Color background, int radius,
            int colorTolerance)
        {
            var foregroundFound = false;
            var backgroundFound = false;
            for (var dy = -radius; dy <= radius; dy++)
            for (var dx = -radius; dx <= radius; dx++)
            {
                if (dx == 0 && dy == 0) continue;
                var nx = centerX + dx;
                var ny = centerY + dy;
                if (nx < 0 || nx >= width || ny < 0 || ny >= height) continue;
                var neighborOffset = ny * rowStride + nx * 4;
                var neighbor = Color.FromArgb(bytes[neighborOffset + 2], bytes[neighborOffset + 1], bytes[neighborOffset]);
                foregroundFound |= ColorDistance(neighbor, foreground) <= colorTolerance;
                backgroundFound |= ColorDistance(neighbor, background) <= colorTolerance;
                if (foregroundFound && backgroundFound) return 2;
            }
            return foregroundFound || backgroundFound ? 1 : 0;
        }

        static int ColorDistance(Color left, Color right) =>
            Math.Abs(left.R - right.R) + Math.Abs(left.G - right.G) + Math.Abs(left.B - right.B);

    }

    private readonly record struct TextSurfaceTransition(
        Color SourceForeground,
        Color SourceBackground,
        Color TargetForeground,
        Color TargetBackground);

    private void DrawFrame(Graphics graphics)
    {
        var targetSurface = _targetMode == ThemeMode.Night ? _nightSurface : _daySurface;
        if (_sourceSurface is not null && _animationSourceSurface is not null &&
            _bridgeSurface is not null && targetSurface is not null && OperatingSystem.IsWindows())
        {
            var destination = graphics.GetHdc();
            try
            {
                if (_progress <= 0D)
                {
                    NativeMethods.BitBlt(destination, 0, 0, Width, Height,
                        _sourceSurface.DeviceContext, 0, 0, NativeMethods.SourceCopyRasterOperation);
                }
                else if (_progress >= 1D)
                {
                    NativeMethods.BitBlt(destination, 0, 0, Width, Height,
                        targetSurface.DeviceContext, 0, 0, NativeMethods.SourceCopyRasterOperation);
                }
                else if (_progress <= 0.5D)
                {
                    // Every intermediate endpoint is generated from the same
                    // immutable source with one neutral alpha-coverage mask.
                    // The bridge keeps foreground and surface roles apart,
                    // while AlphaBlend changes only colour, never geometry.
                    NativeMethods.BitBlt(destination, 0, 0, Width, Height,
                        _animationSourceSurface.DeviceContext, 0, 0,
                        NativeMethods.SourceCopyRasterOperation);
                    NativeMethods.AlphaBlend(destination, 0, 0, Width, Height,
                        _bridgeSurface.DeviceContext, 0, 0, Width, Height,
                        new NativeMethods.BlendFunction
                        {
                            SourceConstantAlpha = (byte)Math.Clamp(
                                (int)Math.Round(_progress * 2D * byte.MaxValue), 0, byte.MaxValue),
                        });
                }
                else
                {
                    NativeMethods.BitBlt(destination, 0, 0, Width, Height,
                        _bridgeSurface.DeviceContext, 0, 0, NativeMethods.SourceCopyRasterOperation);
                    NativeMethods.AlphaBlend(destination, 0, 0, Width, Height,
                        targetSurface.DeviceContext, 0, 0, Width, Height,
                        new NativeMethods.BlendFunction
                        {
                            SourceConstantAlpha = (byte)Math.Clamp(
                                (int)Math.Round((_progress - 0.5D) * 2D * byte.MaxValue),
                                0, byte.MaxValue),
                        });
                }
                return;
            }
            finally
            {
                graphics.ReleaseHdc(destination);
            }
        }

        graphics.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceCopy;
        graphics.CompositingQuality = System.Drawing.Drawing2D.CompositingQuality.HighSpeed;
        graphics.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.NearestNeighbor;
        graphics.PixelOffsetMode = System.Drawing.Drawing2D.PixelOffsetMode.None;
        graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.None;
        graphics.Clear(BackColor);
        if (_source is null) return;
        graphics.DrawImageUnscaled(_source, Point.Empty);
        if (_progress <= 0D) return;

        var target = _targetMode == ThemeMode.Night ? _nightEndpoint : _dayEndpoint;
        var animationSource = _animationSource;
        var bridge = _bridgeEndpoint;
        if (target is null || animationSource is null || bridge is null) return;
        if (_progress >= 1D)
        {
            graphics.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceCopy;
            graphics.DrawImageUnscaled(target, Point.Empty);
            return;
        }

        var firstHalf = _progress <= 0.5D;
        graphics.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceCopy;
        graphics.DrawImageUnscaled(firstHalf ? animationSource : bridge, Point.Empty);
        graphics.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceOver;
        using var attributes = new ImageAttributes();
        attributes.SetColorMatrix(new ColorMatrix
        {
            Matrix33 = (float)(firstHalf ? _progress * 2D : (_progress - 0.5D) * 2D),
        });
        var layer = firstHalf ? bridge : target;
        graphics.DrawImage(layer, ClientRectangle, 0, 0, layer.Width, layer.Height,
            GraphicsUnit.Pixel, attributes);
    }

    internal sealed class GdiSurface : IDisposable
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
                throw new InvalidOperationException("无法创建主题合成内存画布。");
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
