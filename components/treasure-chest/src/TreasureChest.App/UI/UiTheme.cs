using System.Runtime.CompilerServices;

namespace TreasureChest.UI;

internal sealed record ThemeVisualFrame(
    long Id,
    ThemePalette Palette,
    double NightAmount,
    double SelectorPosition,
    ThemeMode TargetMode,
    bool IsTransitioning);

/// <summary>
/// The single product palette for TreasureChest.  The neutral porcelain surfaces,
/// navy structure and lavender accents are sampled from resources/app.png.  Keep
/// state colours deliberately restrained so they support, rather than compete with,
/// the application artwork.
/// </summary>
public static class UiTheme
{
    public const string FontFamily = "Microsoft YaHei UI";
    public const float BodyFontSize = 12F;
    public const float NavigationFontSize = 12F;
    public const float PageTitleFontSize = 22F;
    public const int ButtonHeight = 40;
    public const int SpaceXs = 4;
    public const int SpaceSm = 8;
    public const int SpaceMd = 12;
    public const int SpaceLg = 20;
    public const int PagePadding = 28;

    private static readonly object ThemeAnimationOwner = new();
    private static readonly List<WeakReference<Form>> RegisteredForms = [];
    private static readonly ConditionalWeakTable<Form, object> RegisteredFormSet = new();
    private static readonly ConditionalWeakTable<Control, object> NativeThemeSubscriptions = new();
    private static readonly List<(Form Form, ThemeTransitionOverlay Overlay)> ActiveThemeOverlays = [];
    private static readonly Dictionary<Form, PreparedThemeTransition> PreparedThemeTransitions =
        new(ReferenceEqualityComparer.Instance);
    private static readonly Dictionary<Form, long> ThemePreparationRequests =
        new(ReferenceEqualityComparer.Instance);
    private static ThemePalette? _themeTransitionUnderlyingPalette;
    private static long _themePreparationSequence;
    internal static string? LastCompositeFailureForTests { get; private set; }
    internal static string? LastCompositeTimingForTests { get; private set; }
    internal static double LastCompositePreparationMillisecondsForTests { get; private set; }
    internal static int PreparedTransitionCountForTests => PreparedThemeTransitions.Count;
    internal static int PreparationRequestCountForTests => ThemePreparationRequests.Count;
    internal static int RegisteredFormCountForTests => LiveRegisteredForms().Count;
    internal static int ActiveThemeOverlayCountForTests => ActiveThemeOverlays.Count;
    private static ThemeMode _mode = ThemeMode.Day;
    private static long _frameSequence;
    private static ThemeVisualFrame _frame = new(0, ThemePalette.Day, 0D, 0D, ThemeMode.Day, false);

    public static event EventHandler? PaletteFrameChanged;
    public static event EventHandler? ModeChanged;
    internal static event Action<long, double, int>? FrameCommittedForTests;

    internal static void ReportThemePaint() => AnimationRunner.ReportPaint(ThemeAnimationOwner);

    public static ThemeMode Mode => _mode;
    public static ThemePalette Palette => _frame.Palette;
    internal static ThemeVisualFrame VisualFrame => _frame;
    internal static long VisualFrameId => _frame.Id;
    internal static double NightAmount => _frame.NightAmount;
    internal static double ThemeSelectorPosition => _frame.SelectorPosition;
    internal static bool IsTransitioning => _frame.IsTransitioning;
    public static Color Background => Palette.Background;
    public static Color Surface => Palette.Surface;
    public static Color SurfaceAlt => Palette.SurfaceAlt;
    public static Color SurfaceHover => Palette.SurfaceHover;
    public static Color TitleBar => Palette.TitleBar;
    public static Color Navigation => Palette.Navigation;
    public static Color NavigationHover => Palette.NavigationHover;
    public static Color NavigationPressed => Palette.NavigationPressed;
    public static Color NavigationText => Palette.NavigationText;
    public static Color NavigationMuted => Palette.NavigationMuted;
    public static Color Accent => Palette.Accent;
    public static Color AccentHover => Palette.AccentHover;
    public static Color AccentPressed => Palette.AccentPressed;
    public static Color AccentSoft => Palette.AccentSoft;
    public static Color AccentSoftStrong => Palette.AccentSoftStrong;
    public static Color OnAccent => Palette.OnAccent;
    public static Color Text => Palette.Text;
    public static Color Muted => Palette.Muted;
    public static Color Border => Palette.Border;
    public static Color GridWellBorder => Palette.GridWellBorder;
    public static Color GridLine => Palette.GridLine;
    public static Color DrawerSelected => Palette.DrawerSelected;
    public static Color Divider => Palette.Divider;
    public static Color Running => Palette.Running;
    public static Color Stopped => Palette.Stopped;
    public static Color Warning => Palette.Warning;
    public static Color WarningSurface => Palette.WarningSurface;
    public static Color DisabledBackground => Palette.DisabledBackground;
    public static Color DisabledText => Palette.DisabledText;
    public static Color CloseHover => Palette.CloseHover;
    public static Color ClosePressed => Palette.ClosePressed;
    public static Color ProgressTrack => Palette.ProgressTrack;

    public static ThemeMode ParseMode(string? value) =>
        string.Equals(value?.Trim(), "night", StringComparison.OrdinalIgnoreCase) ? ThemeMode.Night : ThemeMode.Day;

    public static string PersistedName(ThemeMode mode) => mode == ThemeMode.Night ? "night" : "day";

    public static void Initialize(string? persistedMode)
    {
        ThemeConsumerPaintTelemetry.Complete(cancelled: true, endpointPaintIncluded: false);
        AnimationRunner.Cancel(ThemeAnimationOwner);
        _mode = ParseMode(persistedMode);
        var palette = _mode == ThemeMode.Night ? ThemePalette.Night : ThemePalette.Day;
        _frame = new ThemeVisualFrame(
            Interlocked.Increment(ref _frameSequence), palette,
            _mode == ThemeMode.Night ? 1D : 0D,
            _mode == ThemeMode.Night ? 1D : 0D, _mode, false);
        NativeMethods.TrySetPreferredDarkMode(_mode == ThemeMode.Night);
    }

    public static void SetMode(ThemeMode mode, bool animated = true) =>
        SetMode(mode, synchronizedSelector: null, animated);

    internal static void SetMode(
        ThemeMode mode,
        SlidingSegmentedControl? synchronizedSelector,
        bool animated = true)
    {
        if (_mode == mode && Palette == (mode == ThemeMode.Night ? ThemePalette.Night : ThemePalette.Day)) return;
        var from = Palette;
        var fromNightAmount = _frame.NightAmount;
        var target = mode == ThemeMode.Night ? ThemePalette.Night : ThemePalette.Day;
        var targetNightAmount = mode == ThemeMode.Night ? 1D : 0D;
        var selectorStart = synchronizedSelector?.BeginSynchronizedTransition(mode == ThemeMode.Night ? 1 : 0) ?? 0D;
        _mode = mode;
        ModeChanged?.Invoke(null, EventArgs.Empty);
        if (!animated)
        {
            ThemeConsumerPaintTelemetry.Complete(cancelled: true, endpointPaintIncluded: false);
            AnimationRunner.Cancel(ThemeAnimationOwner);
            var treeFrom = _themeTransitionUnderlyingPalette ?? from;
            DiscardThemeOverlays();
            synchronizedSelector?.ApplySynchronizedFrame(mode == ThemeMode.Night ? 1D : 0D);
            PublishAndCommit(treeFrom, target, targetNightAmount, targetNightAmount,
                mode, transitioning: false, applyNative: true);
            return;
        }

        var useCompositeOverlay = BeginOrRetargetThemeOverlays(from, target, mode, synchronizedSelector);
        ThemeConsumerPaintTelemetry.Begin(LiveRegisteredForms(), AnimationTokens.StandardDurationMs);

        AnimationRunner.Start(ThemeAnimationOwner, "palette", AnimationTokens.StandardDurationMs, progress =>
        {
            var previous = Palette;
            var selectorPosition = selectorStart +
                ((mode == ThemeMode.Night ? 1D : 0D) - selectorStart) * progress;
            synchronizedSelector?.ApplySynchronizedFrame(selectorPosition);
            var interpolated = ThemePalette.InterpolateForAnimation(from, target, progress);
            if (useCompositeOverlay && ActiveThemeOverlays.Count > 0)
            {
                PublishFrame(interpolated,
                    fromNightAmount + (targetNightAmount - fromNightAmount) * progress,
                    selectorPosition, mode, transitioning: progress < 1D);
                foreach (var (_, overlay) in ActiveThemeOverlays.ToArray())
                    if (!overlay.IsDisposed) overlay.SetProgress(progress);
            }
            else
            {
                PublishAndCommit(previous, interpolated,
                    fromNightAmount + (targetNightAmount - fromNightAmount) * progress,
                    selectorPosition, mode, transitioning: progress < 1D, applyNative: false);
            }
        }, () =>
        {
            var previous = Palette;
            synchronizedSelector?.ApplySynchronizedFrame(mode == ThemeMode.Night ? 1D : 0D);
            if (useCompositeOverlay && ActiveThemeOverlays.Count > 0)
                CommitCompositeThemeEndpoint(target, targetNightAmount, mode);
            else
                PublishAndCommit(previous, target, targetNightAmount, targetNightAmount,
                    mode, transitioning: false, applyNative: true);
            ThemeConsumerPaintTelemetry.Complete(cancelled: false, endpointPaintIncluded: true);
        });
    }

    private static void PublishFrame(
        ThemePalette palette,
        double nightAmount,
        double selectorPosition,
        ThemeMode targetMode,
        bool transitioning)
    {
        _frame = new ThemeVisualFrame(
            Interlocked.Increment(ref _frameSequence), palette,
            Math.Clamp(nightAmount, 0D, 1D), Math.Clamp(selectorPosition, 0D, 1D),
            targetMode, transitioning);
    }

    private static bool BeginOrRetargetThemeOverlays(
        ThemePalette from,
        ThemePalette target,
        ThemeMode targetMode,
        SlidingSegmentedControl? synchronizedSelector)
    {
        var compositeStartedAt = System.Diagnostics.Stopwatch.GetTimestamp();
        var timingParts = new List<string>();
        LastCompositeTimingForTests = null;
        LastCompositePreparationMillisecondsForTests = 0D;
        ActiveThemeOverlays.RemoveAll(item => item.Form.IsDisposed || item.Overlay.IsDisposed);
        if (ActiveThemeOverlays.Count > 0)
        {
            foreach (var (_, overlay) in ActiveThemeOverlays) overlay.Retarget(targetMode);
            return true;
        }

        var forms = LiveRegisteredForms();
        if (forms.Count == 0) return false;
        var originalFrame = _frame;
        var sourceMode = originalFrame.NightAmount >= 0.5D ? ThemeMode.Night : ThemeMode.Day;
        var sourceFrames = new Dictionary<Form, Bitmap>(ReferenceEqualityComparer.Instance);
        var preparedFrames = new Dictionary<Form,
            (ThemeTransitionOverlay.PreparedFrame Source,
                ThemeTransitionOverlay.PreparedFrame AnimationSource,
                ThemeTransitionOverlay.PreparedFrame Bridge,
                ThemeTransitionOverlay.PreparedFrame Target)>(
                ReferenceEqualityComparer.Instance);
        LastCompositeFailureForTests = null;
        try
        {
            foreach (var form in forms)
            {
                if (TryTakePreparedTransition(form, sourceMode, targetMode,
                        out var preparedSource, out var preparedAnimationSource,
                        out var preparedBridge, out var preparedTarget))
                {
                    preparedFrames[form] = (
                        preparedSource, preparedAnimationSource, preparedBridge, preparedTarget);
                }
                else
                {
                    sourceFrames[form] = ThemeTransitionOverlay.CaptureFormClient(form);
                }
            }

            // Freeze one exact source raster and derive the target by preserving
            // each ClearType channel's coverage while substituting semantic
            // palette roles. Source and target therefore keep the same glyph
            // mask; the real control tree remains untouched until the endpoint.
            foreach (var form in forms)
            {
                var formStartedAt = System.Diagnostics.Stopwatch.GetTimestamp();
                var stableContentRegions = ThemeTransitionOverlay.CaptureStableContentRegions(form);
                var overlay = new ThemeTransitionOverlay();
                try
                {
                    var addStartedAt = System.Diagnostics.Stopwatch.GetTimestamp();
                    form.Controls.Add(overlay);
                    var addMs = System.Diagnostics.Stopwatch.GetElapsedTime(addStartedAt).TotalMilliseconds;
                    var excludedBounds = Rectangle.Empty;
                    if (synchronizedSelector is not null && ReferenceEquals(synchronizedSelector.FindForm(), form))
                    {
                        var origin = form.PointToClient(synchronizedSelector.PointToScreen(Point.Empty));
                        excludedBounds = new Rectangle(origin, synchronizedSelector.ClientSize);
                    }
                    if (preparedFrames.Remove(form, out var prepared))
                    {
                        var beginStartedAt = System.Diagnostics.Stopwatch.GetTimestamp();
                        overlay.BeginPrepared(form, prepared.Source, prepared.AnimationSource, sourceMode,
                            prepared.Bridge, prepared.Target, targetMode, excludedBounds);
                        timingParts.Add($"{form.GetType().Name}:prepared/add={addMs:F2}/begin=" +
                                        $"{System.Diagnostics.Stopwatch.GetElapsedTime(beginStartedAt).TotalMilliseconds:F2}/" +
                                        $"form={System.Diagnostics.Stopwatch.GetElapsedTime(formStartedAt).TotalMilliseconds:F2}ms");
                        prepared.Source.Dispose();
                        prepared.AnimationSource.Dispose();
                        prepared.Bridge.Dispose();
                        prepared.Target.Dispose();
                    }
                    else
                    {
                        var source = sourceFrames[form];
                        var beginStartedAt = System.Diagnostics.Stopwatch.GetTimestamp();
                        overlay.BeginSource(form, source, sourceMode, excludedBounds);
                        var beginMs = System.Diagnostics.Stopwatch.GetElapsedTime(beginStartedAt).TotalMilliseconds;
                        var mapStartedAt = System.Diagnostics.Stopwatch.GetTimestamp();
                        overlay.BuildMappedTargetEndpoint(from, target, targetMode, stableContentRegions);
                        timingParts.Add($"{form.GetType().Name}:fallback/add={addMs:F2}/begin={beginMs:F2}/map=" +
                                        $"{System.Diagnostics.Stopwatch.GetElapsedTime(mapStartedAt).TotalMilliseconds:F2}/" +
                                        $"form={System.Diagnostics.Stopwatch.GetElapsedTime(formStartedAt).TotalMilliseconds:F2}ms");
                        sourceFrames.Remove(form);
                    }
                    ActiveThemeOverlays.Add((form, overlay));
                }
                catch
                {
                    if (ReferenceEquals(overlay.Parent, form)) form.Controls.Remove(overlay);
                    overlay.Dispose();
                    throw;
                }
            }

            _themeTransitionUnderlyingPalette = from;
            LastCompositePreparationMillisecondsForTests =
                System.Diagnostics.Stopwatch.GetElapsedTime(compositeStartedAt).TotalMilliseconds;
            LastCompositeTimingForTests = string.Join(";", timingParts) +
                $";total={LastCompositePreparationMillisecondsForTests:F2}ms";
            return ActiveThemeOverlays.Count > 0;
        }
        catch (Exception error)
        {
            LastCompositeFailureForTests = error.ToString();
            _frame = originalFrame;
            DiscardThemeOverlays();
            return false;
        }
        finally
        {
            _frame = originalFrame;
            foreach (var bitmap in sourceFrames.Values) bitmap.Dispose();
            foreach (var prepared in preparedFrames.Values)
            {
                prepared.Source.Dispose();
                prepared.AnimationSource.Dispose();
                prepared.Bridge.Dispose();
                prepared.Target.Dispose();
            }
        }
    }

    private static void PrepareCompositeEndpointTree(
        IReadOnlyList<Form> forms,
        ThemePalette from,
        ThemePalette to)
    {
        foreach (var form in forms)
            if (form.IsHandleCreated)
                NativeMethods.SendMessage(form.Handle, NativeMethods.WmSetRedraw, IntPtr.Zero, IntPtr.Zero);
        try
        {
            foreach (var form in forms) ApplyThemeTree(form, from, to);
            NativeMethods.TrySetPreferredDarkMode(_mode == ThemeMode.Night);
            foreach (var form in forms)
            {
                ApplyNativeThemeTree(form);
                ApplyWindowChromeTheme(form);
            }
        }
        finally
        {
            foreach (var form in forms)
                if (form.IsHandleCreated)
                    NativeMethods.SendMessage(form.Handle, NativeMethods.WmSetRedraw, (IntPtr)1, IntPtr.Zero);
        }
    }

    private static void CommitCompositeThemeEndpoint(
        ThemePalette target,
        double targetNightAmount,
        ThemeMode targetMode)
    {
        PublishFrame(target, targetNightAmount, targetNightAmount, targetMode, transitioning: false);
        var underlying = _themeTransitionUnderlyingPalette ??
            (targetMode == ThemeMode.Night ? ThemePalette.Day : ThemePalette.Night);
        if (underlying != target)
            ApplyFrameToOpenForms(underlying, target, applyNative: true);
        else
            PaletteFrameChanged?.Invoke(null, EventArgs.Empty);
        var completed = ActiveThemeOverlays.ToArray();
        ActiveThemeOverlays.Clear();
        _themeTransitionUnderlyingPalette = null;
        foreach (var (form, overlay) in completed)
        {
            if (overlay.IsDisposed) continue;
            overlay.Finish();
            form.Invalidate(true);
            try
            {
                form.BeginInvoke(() =>
                {
                    if (overlay.IsDisposed) return;
                    form.Controls.Remove(overlay);
                    overlay.Dispose();
                });
            }
            catch (InvalidOperationException)
            {
                form.Controls.Remove(overlay);
                overlay.Dispose();
            }
        }
    }

    private static void DiscardThemeOverlays()
    {
        foreach (var (form, overlay) in ActiveThemeOverlays.ToArray())
        {
            if (overlay.IsDisposed) continue;
            overlay.Finish();
            form.Controls.Remove(overlay);
            overlay.Dispose();
        }
        ActiveThemeOverlays.Clear();
        _themeTransitionUnderlyingPalette = null;
    }

    public static void ApplyCurrentTheme(Control root)
    {
        ArgumentNullException.ThrowIfNull(root);
        var opposite = _mode == ThemeMode.Night ? ThemePalette.Day : ThemePalette.Night;
        ApplyThemeTree(root, opposite, Palette);
        ApplyInputsRecursively(root);
        root.Invalidate(true);
    }

    public static void ConfigureDpiAwareForm(Form form, bool manuallyManaged = false)
    {
        form.Font = CreateFont();
        form.ForeColor = Text;
        form.BackColor = Background;
        AttachControlTheme(form);
        RegisterForm(form);
        if (manuallyManaged)
        {
            form.AutoScaleMode = AutoScaleMode.None;
            return;
        }
        form.AutoScaleDimensions = new SizeF(96F, 96F);
        form.AutoScaleMode = AutoScaleMode.Dpi;
    }

    public static Button Button(string text, bool primary = false, ButtonGlyph glyph = ButtonGlyph.None)
    {
        var button = new RoundedButton
        {
            Text = text,
            Glyph = glyph,
            AutoSize = true,
            MinimumSize = new Size(0, ButtonHeight),
            Padding = new Padding(14, 0, 14, 0),
            FlatStyle = FlatStyle.Flat,
            BackColor = primary ? Accent : Surface,
            ForeColor = primary ? OnAccent : Text,
            Cursor = Cursors.Hand,
            Font = CreateFont(),
            UseVisualStyleBackColor = false,
        };
        ApplyButtonState(button, primary);
        button.EnabledChanged += (_, _) => ApplyButtonState(button, primary);
        return button;
    }

    public static Label Heading(string text, float size = 18F) => new()
    {
        Text = text,
        AutoSize = true,
        Font = CreateFont(size, FontStyle.Bold),
        ForeColor = Text,
    };

    public static void StyleMenu(ContextMenuStrip menu)
    {
        menu.BackColor = Surface;
        menu.ForeColor = Text;
        menu.Font = CreateFont();
        menu.RenderMode = ToolStripRenderMode.Professional;
        menu.Renderer = new ToolStripProfessionalRenderer(new ThemeColorTable());
    }

    public static Font CreateFont(float size = BodyFontSize, FontStyle style = FontStyle.Regular) =>
        new(FontFamily, size, style);

    public static double ContrastRatio(Color foreground, Color background)
    {
        static double Luminance(Color color)
        {
            static double Channel(byte value)
            {
                var component = value / 255D;
                return component <= 0.04045D
                    ? component / 12.92D
                    : Math.Pow((component + 0.055D) / 1.055D, 2.4D);
            }

            return (0.2126D * Channel(color.R)) +
                   (0.7152D * Channel(color.G)) +
                   (0.0722D * Channel(color.B));
        }

        var foregroundLuminance = Luminance(foreground);
        var backgroundLuminance = Luminance(background);
        return (Math.Max(foregroundLuminance, backgroundLuminance) + 0.05D) /
               (Math.Min(foregroundLuminance, backgroundLuminance) + 0.05D);
    }

    private static void ApplyButtonState(Button button, bool primary)
    {
        if (!button.Enabled)
        {
            button.BackColor = DisabledBackground;
            button.ForeColor = DisabledText;
            button.FlatAppearance.BorderColor = Border;
            button.FlatAppearance.MouseOverBackColor = DisabledBackground;
            button.FlatAppearance.MouseDownBackColor = DisabledBackground;
            return;
        }

        button.BackColor = primary ? Accent : Surface;
        button.ForeColor = primary ? OnAccent : Text;
        button.FlatAppearance.BorderColor = primary ? Accent : Border;
        button.FlatAppearance.MouseOverBackColor = primary ? AccentHover : SurfaceHover;
        button.FlatAppearance.MouseDownBackColor = primary ? AccentPressed : AccentSoftStrong;
    }

    private static void AttachControlTheme(Control root)
    {
        root.ControlAdded += (_, eventArgs) =>
        {
            if (eventArgs.Control is null) return;
            ApplyInputTheme(eventArgs.Control);
            AttachControlTheme(eventArgs.Control);
        };

        ApplyInputTheme(root);
        foreach (Control child in root.Controls) AttachControlTheme(child);
    }

    private static void ApplyInputTheme(Control control)
    {
        // WinForms containers created before they are attached to a themed form keep
        // SystemColors.ControlText.  Child labels then inherit that fixed black value,
        // which becomes unreadable after a live switch to the night palette.  Normalize
        // the framework default once; explicit semantic/status colours are left alone.
        if (control.ForeColor.ToArgb() == SystemColors.ControlText.ToArgb())
            control.ForeColor = Text;

        switch (control)
        {
            case Label label:
                // Respect Windows' native text rasterization. Forcing the
                // compatible GDI+ path affected ordinary Chinese descriptions
                // as well as action-button text in user screenshots.
                label.UseCompatibleTextRendering = false;
                break;
            case TextBoxBase textBox:
                textBox.BackColor = Surface;
                textBox.ForeColor = Text;
                textBox.BorderStyle = textBox is IThemedEmbeddedEditor ? BorderStyle.None : BorderStyle.FixedSingle;
                break;
            case ComboBox comboBox:
                comboBox.BackColor = Surface;
                comboBox.ForeColor = Text;
                comboBox.FlatStyle = FlatStyle.Flat;
                break;
            case ListBox listBox:
                listBox.BackColor = Surface;
                listBox.ForeColor = Text;
                break;
            case UpDownBase upDown:
                upDown.BackColor = Surface;
                upDown.ForeColor = Text;
                upDown.BorderStyle = BorderStyle.FixedSingle;
                break;
            case CheckBox checkBox:
                checkBox.ForeColor = Text;
                break;
            case RadioButton radioButton:
                radioButton.ForeColor = Text;
                break;
        }
        EnsureNativeTheme(control);
    }

    private static void ApplyInputsRecursively(Control root)
    {
        ApplyInputTheme(root);
        foreach (Control child in root.Controls) ApplyInputsRecursively(child);
    }

    private static void RegisterForm(Form form)
    {
        if (RegisteredFormSet.TryGetValue(form, out _)) return;
        RegisteredFormSet.Add(form, new object());
        RegisteredForms.Add(new WeakReference<Form>(form));
        form.HandleCreated += (_, _) => ApplyWindowChromeTheme(form);
        form.VisibleChanged += (_, _) =>
        {
            if (form.Visible && !form.IsDisposed) ApplyCurrentTheme(form);
        };
        if (form.IsHandleCreated) ApplyWindowChromeTheme(form);
        form.FormClosed += FormLifecycleEnded;
        form.Disposed += FormLifecycleEnded;
    }

    private static void FormLifecycleEnded(object? sender, EventArgs e)
    {
        if (sender is Form form) UnregisterForm(form);
    }

    private static void UnregisterForm(Form form)
    {
        // FormClosed is not guaranteed when a constructor/test owner calls Dispose
        // directly.  The weak registry itself does not retain that form, but the
        // pre-render request and prepared-frame dictionaries intentionally use
        // strong keys while a window is live.  Clear every owned theme resource
        // from both lifecycle endpoints; the operation is deliberately idempotent
        // because FormClosed is normally followed by Disposed.
        RegisteredFormSet.Remove(form);
        ThemePreparationRequests.Remove(form);
        if (PreparedThemeTransitions.Remove(form, out var prepared)) prepared.Dispose();
        for (var index = ActiveThemeOverlays.Count - 1; index >= 0; index--)
        {
            var (candidate, overlay) = ActiveThemeOverlays[index];
            if (!ReferenceEquals(candidate, form)) continue;
            ActiveThemeOverlays.RemoveAt(index);
            if (!overlay.IsDisposed)
            {
                overlay.Finish();
                overlay.Parent?.Controls.Remove(overlay);
                overlay.Dispose();
            }
        }
        if (ActiveThemeOverlays.Count == 0) _themeTransitionUnderlyingPalette = null;
        for (var index = RegisteredForms.Count - 1; index >= 0; index--)
        {
            if (!RegisteredForms[index].TryGetTarget(out var candidate) || ReferenceEquals(candidate, form))
                RegisteredForms.RemoveAt(index);
        }
    }

    /// <summary>
    /// Builds the expensive screenshot and semantic palette-mapping pair
    /// while the settings page is already stable.  A theme click can then show
    /// the immutable compositor immediately instead of doing a full-window
    /// pixel pass before the 220 ms timeline even starts.
    /// </summary>
    internal static void PrimeTransitionFrames(bool force = false)
    {
        if (IsTransitioning || ActiveThemeOverlays.Count > 0) return;
        foreach (var form in LiveRegisteredForms())
        {
            if (form.InvokeRequired)
            {
                try { form.BeginInvoke(() => PrimeTransitionFrames(force)); }
                catch (InvalidOperationException) { }
                continue;
            }
            PrimeTransitionFrame(form, force);
        }
    }

    private static void PrimeTransitionFrame(Form form, bool force)
    {
        if (form.IsDisposed || !form.Visible || !form.IsHandleCreated || IsTransitioning) return;
        var sourceMode = _frame.NightAmount >= 0.5D ? ThemeMode.Night : ThemeMode.Day;
        var targetMode = sourceMode == ThemeMode.Day ? ThemeMode.Night : ThemeMode.Day;
        if (!force && PreparedThemeTransitions.TryGetValue(form, out var existing) &&
            existing.Matches(form.ClientSize, sourceMode, targetMode))
            return;

        var request = Interlocked.Increment(ref _themePreparationSequence);
        ThemePreparationRequests[form] = request;
        var clientSize = form.ClientSize;
        Bitmap source;
        try
        {
            source = ThemeTransitionOverlay.CaptureFormClient(form);
        }
        catch (Exception error) when (error is ArgumentException or InvalidOperationException or
                                      System.Runtime.InteropServices.ExternalException)
        {
            LastCompositeFailureForTests = error.ToString();
            return;
        }

        var sourcePalette = sourceMode == ThemeMode.Day ? ThemePalette.Day : ThemePalette.Night;
        var targetPalette = targetMode == ThemeMode.Day ? ThemePalette.Day : ThemePalette.Night;
        var stableContentRegions = ThemeTransitionOverlay.CaptureStableContentRegions(form);
        _ = Task.Run(() =>
        {
            Bitmap? preparedSource = source;
            Bitmap? preparedAnimationSource = null;
            Bitmap? preparedBridge = null;
            Bitmap? preparedTarget = null;
            try
            {
                preparedAnimationSource = ThemeTransitionOverlay.MapPaletteRolesForAnimation(
                    preparedSource, sourcePalette, sourcePalette, normalizeTextSubpixelCoverage: true,
                    stableContentRegions: stableContentRegions);
                preparedBridge = ThemeTransitionOverlay.MapPaletteRolesForAnimation(
                    preparedSource, sourcePalette, ThemePalette.AnimationBridge,
                    normalizeTextSubpixelCoverage: true, stableContentRegions: stableContentRegions);
                preparedTarget = ThemeTransitionOverlay.MapPaletteRolesForAnimation(
                    preparedSource, sourcePalette, targetPalette, normalizeTextSubpixelCoverage: true,
                    stableContentRegions: stableContentRegions);
            }
            catch
            {
                preparedSource?.Dispose();
                preparedAnimationSource?.Dispose();
                preparedBridge?.Dispose();
                preparedTarget?.Dispose();
                return;
            }

            void CommitPrepared()
            {
                if (form.IsDisposed || !form.Visible || !form.IsHandleCreated || IsTransitioning ||
                    !ThemePreparationRequests.TryGetValue(form, out var currentRequest) ||
                    currentRequest != request || form.ClientSize != clientSize ||
                    (_frame.NightAmount >= 0.5D ? ThemeMode.Night : ThemeMode.Day) != sourceMode)
                {
                    preparedSource?.Dispose();
                    preparedAnimationSource?.Dispose();
                    preparedBridge?.Dispose();
                    preparedTarget?.Dispose();
                    return;
                }

                ThemeTransitionOverlay.PreparedFrame? preparedSourceFrame = null;
                ThemeTransitionOverlay.PreparedFrame? preparedAnimationSourceFrame = null;
                ThemeTransitionOverlay.PreparedFrame? preparedBridgeFrame = null;
                ThemeTransitionOverlay.PreparedFrame? preparedTargetFrame = null;
                try
                {
                    preparedSourceFrame = new ThemeTransitionOverlay.PreparedFrame(preparedSource!);
                    preparedSource = null;
                    preparedAnimationSourceFrame = new ThemeTransitionOverlay.PreparedFrame(preparedAnimationSource!);
                    preparedAnimationSource = null;
                    preparedBridgeFrame = new ThemeTransitionOverlay.PreparedFrame(preparedBridge!);
                    preparedBridge = null;
                    preparedTargetFrame = new ThemeTransitionOverlay.PreparedFrame(preparedTarget!);
                    preparedTarget = null;
                    if (PreparedThemeTransitions.Remove(form, out var previous)) previous.Dispose();
                    PreparedThemeTransitions[form] = new PreparedThemeTransition(
                        preparedSourceFrame, preparedAnimationSourceFrame, preparedBridgeFrame, preparedTargetFrame,
                        clientSize, sourceMode, targetMode, DateTimeOffset.UtcNow);
                    preparedSourceFrame = null;
                    preparedAnimationSourceFrame = null;
                    preparedBridgeFrame = null;
                    preparedTargetFrame = null;
                }
                catch (Exception error) when (error is ArgumentException or InvalidOperationException or
                                              System.Runtime.InteropServices.ExternalException)
                {
                    preparedSourceFrame?.Dispose();
                    preparedAnimationSourceFrame?.Dispose();
                    preparedBridgeFrame?.Dispose();
                    preparedTargetFrame?.Dispose();
                    preparedSource?.Dispose();
                    preparedAnimationSource?.Dispose();
                    preparedBridge?.Dispose();
                    preparedTarget?.Dispose();
                    LastCompositeFailureForTests = error.ToString();
                }
            }

            try { form.BeginInvoke(CommitPrepared); }
            catch (Exception error) when (error is InvalidOperationException or ObjectDisposedException)
            {
                preparedSource?.Dispose();
                preparedAnimationSource?.Dispose();
                preparedBridge?.Dispose();
                preparedTarget?.Dispose();
            }
        });
    }

    private static bool TryTakePreparedTransition(
        Form form,
        ThemeMode sourceMode,
        ThemeMode targetMode,
        out ThemeTransitionOverlay.PreparedFrame source,
        out ThemeTransitionOverlay.PreparedFrame animationSource,
        out ThemeTransitionOverlay.PreparedFrame bridge,
        out ThemeTransitionOverlay.PreparedFrame target)
    {
        source = null!;
        animationSource = null!;
        bridge = null!;
        target = null!;
        if (!PreparedThemeTransitions.Remove(form, out var prepared)) return false;
        if (!prepared.Matches(form.ClientSize, sourceMode, targetMode))
        {
            prepared.Dispose();
            return false;
        }
        (source, animationSource, bridge, target) = prepared.Take();
        return true;
    }

    private static IReadOnlyList<Form> LiveRegisteredForms()
    {
        var forms = new List<Form>();
        for (var index = RegisteredForms.Count - 1; index >= 0; index--)
        {
            if (!RegisteredForms[index].TryGetTarget(out var form) || form.IsDisposed)
            {
                RegisteredForms.RemoveAt(index);
                continue;
            }
            if (form.Visible && form.IsHandleCreated) forms.Add(form);
        }
        return forms;
    }

    private static void PublishAndCommit(
        ThemePalette from,
        ThemePalette to,
        double nightAmount,
        double selectorPosition,
        ThemeMode targetMode,
        bool transitioning,
        bool applyNative)
    {
        _frame = new ThemeVisualFrame(
            Interlocked.Increment(ref _frameSequence), to,
            Math.Clamp(nightAmount, 0D, 1D), Math.Clamp(selectorPosition, 0D, 1D),
            targetMode, transitioning);
        ApplyFrameToOpenForms(from, to, applyNative);
    }

    private static void ApplyFrameToOpenForms(ThemePalette from, ThemePalette to, bool applyNative)
    {
        var commitStartedAt = System.Diagnostics.Stopwatch.GetTimestamp();
        var forms = new List<Form>();
        for (var index = RegisteredForms.Count - 1; index >= 0; index--)
        {
            if (!RegisteredForms[index].TryGetTarget(out var form) || form.IsDisposed)
            {
                RegisteredForms.RemoveAt(index);
                continue;
            }
            if (applyNative || form.Visible) forms.Add(form);
        }

        foreach (var form in forms)
            if (form.IsHandleCreated)
                NativeMethods.SendMessage(form.Handle, NativeMethods.WmSetRedraw, IntPtr.Zero, IntPtr.Zero);
        try
        {
            foreach (var form in forms) ApplyThemeTree(form, from, to, visibleOnly: !applyNative);
            if (applyNative)
            {
                NativeMethods.TrySetPreferredDarkMode(_mode == ThemeMode.Night);
                foreach (var form in forms)
                {
                    ApplyNativeThemeTree(form);
                    ApplyWindowChromeTheme(form);
                }
            }
            PaletteFrameChanged?.Invoke(null, EventArgs.Empty);
        }
        finally
        {
            foreach (var form in forms)
            {
                if (!form.IsHandleCreated) continue;
                NativeMethods.SendMessage(form.Handle, NativeMethods.WmSetRedraw, (IntPtr)1, IntPtr.Zero);
                NativeMethods.RedrawWindow(
                    form.Handle,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    NativeMethods.RdwInvalidate | NativeMethods.RdwErase | NativeMethods.RdwFrame |
                    NativeMethods.RdwAllChildren | NativeMethods.RdwUpdateNow);
            }
            FrameCommittedForTests?.Invoke(
                _frame.Id,
                System.Diagnostics.Stopwatch.GetElapsedTime(commitStartedAt).TotalMilliseconds,
                forms.Count);
        }
    }

    private static void ApplyThemeTree(
        Control control,
        ThemePalette from,
        ThemePalette to,
        bool visibleOnly = false)
    {
        control.BackColor = Map(control.BackColor, from, to);
        control.ForeColor = Map(control.ForeColor, from, to);
        switch (control)
        {
            case Button button:
                button.FlatAppearance.BorderColor = Map(button.FlatAppearance.BorderColor, from, to);
                button.FlatAppearance.MouseOverBackColor = Map(button.FlatAppearance.MouseOverBackColor, from, to);
                button.FlatAppearance.MouseDownBackColor = Map(button.FlatAppearance.MouseDownBackColor, from, to);
                break;
            case DataGridView grid:
                // DataGridView materializes system-white row styles lazily.  Mapping only
                // colours that happened to exist in the previous palette therefore leaves
                // detached grids with white rows in night mode (or dark rows in day mode).
                // Base grid roles are known, so assign them from the current frame directly;
                // row/cell overrides below still map semantic drawer/status colours.
                grid.BackgroundColor = to.Surface;
                grid.GridColor = to.GridLine;
                SetGridBodyStyle(grid.DefaultCellStyle, to);
                SetGridBodyStyle(grid.RowsDefaultCellStyle, to);
                SetGridBodyStyle(grid.AlternatingRowsDefaultCellStyle, to);
                SetGridBodyStyle(grid.RowTemplate.DefaultCellStyle, to);
                SetGridHeaderStyle(grid.ColumnHeadersDefaultCellStyle, to);
                SetGridHeaderStyle(grid.RowHeadersDefaultCellStyle, to);
                foreach (DataGridViewColumn column in grid.Columns) MapStyle(column.DefaultCellStyle, from, to);
                foreach (DataGridViewRow row in grid.Rows)
                {
                    MapStyle(row.DefaultCellStyle, from, to);
                    foreach (DataGridViewCell cell in row.Cells) MapStyle(cell.Style, from, to);
                }
                grid.Invalidate(true);
                break;
            case ContextMenuStrip menu:
                menu.Renderer = new ToolStripProfessionalRenderer(new ThemeColorTable());
                break;
        }
        EnsureNativeTheme(control, applyNow: false);
        foreach (Control child in control.Controls)
        {
            // MainForm caches each page instead of repeatedly leaking and rebuilding
            // controls. Hidden cached pages are not part of the current visual frame;
            // recolouring them 30-40 times per transition only delays the visible tree.
            // ShowPage reapplies the immutable endpoint before exposing a cached page.
            if (visibleOnly && !child.Visible) continue;
            ApplyThemeTree(child, from, to, visibleOnly);
        }
    }

    private static void EnsureNativeTheme(Control control, bool applyNow = true)
    {
        if (!RequiresNativeTheme(control)) return;
        if (!NativeThemeSubscriptions.TryGetValue(control, out _))
        {
            NativeThemeSubscriptions.Add(control, new object());
            control.HandleCreated += (_, _) => ApplyNativeTheme(control);
        }
        if (applyNow && control.IsHandleCreated) ApplyNativeTheme(control);
    }

    private static void ApplyNativeThemeTree(Control control)
    {
        if (RequiresNativeTheme(control)) ApplyNativeTheme(control);
        foreach (Control child in control.Controls) ApplyNativeThemeTree(child);
    }

    internal static bool RequiresNativeTheme(Control control) =>
        control is not IThemedEmbeddedEditor and not ThemedComboBox &&
        (control is DataGridView or TextBoxBase or ComboBox or ListBox or UpDownBase or ScrollBar ||
         control is ScrollableControl { AutoScroll: true });

    private static void ApplyNativeTheme(Control control)
    {
        if (control.IsDisposed || !control.IsHandleCreated) return;
        var themeClass = NativeThemeClass(control, _mode);
        if (themeClass is null) return;
        NativeMethods.SetWindowTheme(control.Handle, themeClass, null);
    }

    internal static string? NativeThemeClass(Control control, ThemeMode mode)
    {
        // These controls retain native HWNDs for text input, IME, drop-down and
        // accessibility behavior, but their visible chrome is composed by the
        // product. Applying DarkMode_CFD here lets the native child repaint a
        // non-client tail/arrow after the owner-painted surface, which is exactly
        // the black rectangle visible in real Manager WGC frames.
        if (control is IThemedEmbeddedEditor or ThemedComboBox) return null;
        if (mode == ThemeMode.Day) return "Explorer";
        return control is ComboBox or TextBoxBase or UpDownBase ? "DarkMode_CFD" : "DarkMode_Explorer";
    }

    private static void ApplyWindowChromeTheme(Form form)
    {
        if (form.IsDisposed || !form.IsHandleCreated) return;
        NativeMethods.TrySetWindowDarkMode(form.Handle, _mode == ThemeMode.Night);
    }

    private static void MapStyle(DataGridViewCellStyle style, ThemePalette from, ThemePalette to)
    {
        style.BackColor = Map(style.BackColor, from, to);
        style.ForeColor = Map(style.ForeColor, from, to);
        style.SelectionBackColor = Map(style.SelectionBackColor, from, to);
        style.SelectionForeColor = Map(style.SelectionForeColor, from, to);
    }

    private static void SetGridBodyStyle(DataGridViewCellStyle style, ThemePalette palette)
    {
        style.BackColor = palette.Surface;
        style.ForeColor = palette.Text;
        style.SelectionBackColor = palette.AccentSoftStrong;
        style.SelectionForeColor = palette.Text;
    }

    private static void SetGridHeaderStyle(DataGridViewCellStyle style, ThemePalette palette)
    {
        style.BackColor = palette.SurfaceAlt;
        style.ForeColor = palette.Text;
        style.SelectionBackColor = palette.SurfaceAlt;
        style.SelectionForeColor = palette.Text;
    }

    private static Color Map(Color value, ThemePalette from, ThemePalette to)
    {
        if (value.IsEmpty || value == Color.Transparent) return value;
        var fromColors = from.Colors;
        var toColors = to.Colors;
        for (var index = 0; index < fromColors.Count; index++)
            if (value.ToArgb() == fromColors[index].ToArgb()) return toColors[index];
        return value;
    }

    private sealed class PreparedThemeTransition(
        ThemeTransitionOverlay.PreparedFrame source,
        ThemeTransitionOverlay.PreparedFrame animationSource,
        ThemeTransitionOverlay.PreparedFrame bridge,
        ThemeTransitionOverlay.PreparedFrame target,
        Size clientSize,
        ThemeMode sourceMode,
        ThemeMode targetMode,
        DateTimeOffset preparedAt) : IDisposable
    {
        private ThemeTransitionOverlay.PreparedFrame? _source = source;
        private ThemeTransitionOverlay.PreparedFrame? _animationSource = animationSource;
        private ThemeTransitionOverlay.PreparedFrame? _bridge = bridge;
        private ThemeTransitionOverlay.PreparedFrame? _target = target;

        internal DateTimeOffset PreparedAt { get; } = preparedAt;

        internal bool Matches(Size size, ThemeMode from, ThemeMode to) =>
            _source is not null && _animationSource is not null && _bridge is not null &&
            _target is not null && clientSize == size &&
            sourceMode == from && targetMode == to;

        internal (ThemeTransitionOverlay.PreparedFrame Source,
            ThemeTransitionOverlay.PreparedFrame AnimationSource,
            ThemeTransitionOverlay.PreparedFrame Bridge,
            ThemeTransitionOverlay.PreparedFrame Target) Take()
        {
            if (_source is null || _animationSource is null || _bridge is null || _target is null)
                throw new InvalidOperationException("主题预热画面已经被消费。");
            var result = (_source, _animationSource, _bridge, _target);
            _source = null;
            _animationSource = null;
            _bridge = null;
            _target = null;
            return result;
        }

        public void Dispose()
        {
            _source?.Dispose();
            _animationSource?.Dispose();
            _bridge?.Dispose();
            _target?.Dispose();
            _source = null;
            _animationSource = null;
            _bridge = null;
            _target = null;
        }
    }

    private sealed class ThemeColorTable : ProfessionalColorTable
    {
        public override Color ToolStripDropDownBackground => Surface;
        public override Color ImageMarginGradientBegin => SurfaceAlt;
        public override Color ImageMarginGradientMiddle => SurfaceAlt;
        public override Color ImageMarginGradientEnd => SurfaceAlt;
        public override Color MenuBorder => Border;
        public override Color MenuItemBorder => AccentSoftStrong;
        public override Color MenuItemSelected => AccentSoft;
        public override Color MenuItemSelectedGradientBegin => AccentSoft;
        public override Color MenuItemSelectedGradientEnd => AccentSoft;
        public override Color MenuItemPressedGradientBegin => AccentSoftStrong;
        public override Color MenuItemPressedGradientEnd => AccentSoftStrong;
        public override Color SeparatorDark => Border;
        public override Color SeparatorLight => Surface;
    }
}
