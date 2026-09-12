using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace TreasureChest.UI;

/// <summary>
/// Marks a control whose overridden OnPaint reports its completed frame
/// explicitly. AnimationRunner must not also subscribe to Control.Paint for
/// such a control or one physical paint can be counted twice.
/// </summary>
public interface IExplicitAnimationPaintSource
{
    bool IncludeInThemePaintTelemetry => true;
}

public static class AnimationTokens
{
    public const int ShortDurationMs = 160;
    public const int StandardDurationMs = 220;
    public const int ComplexDurationMs = 280;
    public const int MaximumDurationMs = 300;

    public static bool Enabled { get; set; } = true;

    public static double EaseInOut(double value)
    {
        var t = Math.Clamp(value, 0D, 1D);
        return t * t * t * (t * (t * 6D - 15D) + 10D);
    }
}

/// <summary>
/// One UI-thread timeline for the whole product.  Every animation is keyed, so a
/// repeated click replaces the preceding run and starts from the caller's current
/// visual state instead of stacking another Timer or jumping back to an endpoint.
/// </summary>
public static class AnimationRunner
{
    private sealed class DispatcherControl : Control
    {
        protected override void SetVisibleCore(bool value) => base.SetVisibleCore(false);

        public void EnsureDispatcherHandle() => _ = Handle;
    }

    private sealed class Entry
    {
        public required object Owner { get; init; }
        public required string Key { get; init; }
        public required long StartedAt { get; init; }
        public required int DurationMs { get; init; }
        public required Action<double> Frame { get; init; }
        public Action? Completed { get; init; }
        public Action? Cancelled { get; init; }
        public int TargetRefreshHz { get; init; }
        public List<long> PaintTimestamps { get; } = [];
        public PaintEventHandler? PaintHandler { get; set; }
        public bool AwaitingEndpointPaint { get; set; }
        public bool EndpointCompletionQueued { get; set; }
        public bool EndpointPaintObserved { get; set; }
        public long EndpointRequestedAt { get; set; }
    }

    private static readonly List<Entry> Entries = [];
    private static readonly Queue<AnimationPaintStatistics> CompletedStatistics = new();
    private static readonly CompositionFramePump FramePump = new(RequestUiFrameFromWorker);
    private static DispatcherControl? _dispatcher;
    private static int _uiThreadId;
    private static int _uiFramePending;
    private static bool _applicationExitHooked;

    public static bool IsFramePumpRunning => FramePump.IsRunning;
    public static int PendingUiFrameCount => Volatile.Read(ref _uiFramePending);
    public static AnimationPaintStatistics? LastPaintStatistics =>
        CompletedStatistics.Count == 0 ? null : CompletedStatistics.Last();
    internal static int FramePumpWorkerCount => FramePump.WorkerCount;
    internal static int FramePumpOutstandingCancellationSources => FramePump.OutstandingCancellationSources;
    internal static int FramePumpNativeTimerCount => HighResolutionFrameWaiter.ActiveNativeTimerCount;
    internal static int FramePumpCancellationWaitHandleCount => HighResolutionFrameWaiter.ActiveCancellationWaitHandleCount;
    internal static Exception? FramePumpLastFailure => FramePump.LastWorkerFailure;
    internal static void ShutdownDispatcherForTests()
    {
        if (IsUiThread) ShutdownOnUi();
    }
    internal static int DisplayConfigurationGeneration { get; private set; }
    // Visible-control SelfTest runs without Application.Run.  When enabled only
    // by that test, consume the invalid region immediately after the real
    // compositor tick so telemetry still records the control's completed
    // OnPaint path rather than an Invalidate call. Product code never enables it.
    internal static bool ConsumeInvalidRegionSynchronouslyForTest { get; set; }
    internal static int SynchronousTestRefreshCount { get; private set; }
    internal static int SynchronousTestPaintReportCount { get; private set; }
    internal static void ResetSynchronousTestCounters()
    {
        SynchronousTestRefreshCount = 0;
        SynchronousTestPaintReportCount = 0;
    }

    public static void Start(
        object owner,
        string key,
        int durationMs,
        Action<double> frame,
        Action? completed = null)
    {
        ArgumentNullException.ThrowIfNull(owner);
        ArgumentException.ThrowIfNullOrWhiteSpace(key);
        ArgumentNullException.ThrowIfNull(frame);
        if (durationMs is < 1 or > AnimationTokens.MaximumDurationMs)
            throw new ArgumentOutOfRangeException(nameof(durationMs), $"动画时长必须为 1 至 {AnimationTokens.MaximumDurationMs}ms。");

        Validate(owner, key, durationMs, frame);
        if (!TryRunOnUi(owner, () => StartCore(owner, key, durationMs, frame, completed, null)))
            throw new InvalidOperationException("动画必须从已建立的 WinForms UI 序列启动。");
    }

    public static Task RunAsync(
        object owner,
        string key,
        int durationMs,
        Action<double> frame,
        CancellationToken cancellationToken = default)
    {
        Validate(owner, key, durationMs, frame);
        var state = new AsyncRunState(cancellationToken);
        if (cancellationToken.CanBeCanceled)
        {
            state.Registration = cancellationToken.Register(static value =>
            {
                var asyncState = (AsyncRunState)value!;
                asyncState.CancellationRequested = true;
                PostToUi(() =>
                {
                    if (asyncState.Entry is { } entry) CancelExactCore(entry);
                    else asyncState.Completion.TrySetCanceled(asyncState.Token);
                });
            }, state);
        }

        if (!TryRunOnUi(owner, () =>
        {
            if (state.CancellationRequested || state.Token.IsCancellationRequested)
            {
                state.Completion.TrySetCanceled(state.Token);
                return;
            }

            state.Entry = StartCore(owner, key, durationMs, frame,
                () => state.Completion.TrySetResult(),
                () => state.Completion.TrySetCanceled(state.Token.IsCancellationRequested
                    ? state.Token
                    : new CancellationToken(canceled: true)));
        }))
        {
            state.Registration.Dispose();
            throw new InvalidOperationException("异步动画必须从已建立的 WinForms UI 序列启动。");
        }

        return AwaitAndDisposeAsync(state);
    }

    public static bool Cancel(object owner, string? key = null)
    {
        ArgumentNullException.ThrowIfNull(owner);
        if (IsUiThread) return CancelCore(owner, key);
        if (_dispatcher is null || _dispatcher.IsDisposed) return false;
        PostToUi(() => CancelCore(owner, key));
        return true;
    }

    /// <summary>
    /// Records a frame only after the animated control has actually completed its
    /// paint callback.  Invalidates and background frame requests are intentionally
    /// excluded from the telemetry.
    /// </summary>
    public static void ReportPaint(object owner, long timestamp = 0)
    {
        if (ConsumeInvalidRegionSynchronouslyForTest) SynchronousTestPaintReportCount++;
        var paintedAt = timestamp == 0 ? Stopwatch.GetTimestamp() : timestamp;
        ThemeConsumerPaintTelemetry.Report(owner, paintedAt);
        if (!IsUiThread || Entries.Count == 0) return;
        foreach (var entry in Entries.ToArray())
        {
            if (!ReferenceEquals(entry.Owner, owner)) continue;
            if (entry.PaintTimestamps.Count > 0 &&
                Stopwatch.GetElapsedTime(entry.PaintTimestamps[^1], paintedAt).TotalMilliseconds < 1D)
                entry.PaintTimestamps[^1] = paintedAt;
            else
                entry.PaintTimestamps.Add(paintedAt);
            if (!entry.AwaitingEndpointPaint || entry.EndpointCompletionQueued) continue;
            entry.EndpointPaintObserved = true;
            entry.EndpointCompletionQueued = true;
            PostToUi(() => CompleteEndpointCore(entry));
        }
    }

    internal static int NormalizeTargetRefreshHz(int value) => Math.Clamp(value <= 1 ? 60 : value, 60, 240);
    internal static int DetectTargetRefreshHz(Control? owner) => NativeRefreshRate.Detect(owner);
    internal static void InvalidateDisplayRefreshRate() => DisplayConfigurationGeneration++;

    private static bool IsUiThread => _uiThreadId != 0 && Environment.CurrentManagedThreadId == _uiThreadId;

    private static void Validate(object owner, string key, int durationMs, Action<double> frame)
    {
        ArgumentNullException.ThrowIfNull(owner);
        ArgumentException.ThrowIfNullOrWhiteSpace(key);
        ArgumentNullException.ThrowIfNull(frame);
        if (durationMs is < 1 or > AnimationTokens.MaximumDurationMs)
            throw new ArgumentOutOfRangeException(nameof(durationMs), $"动画时长必须为 1 至 {AnimationTokens.MaximumDurationMs}ms。");
    }

    private static bool TryRunOnUi(object owner, Action action)
    {
        if (owner is Control control && !control.IsDisposed && !control.InvokeRequired)
        {
            if (_dispatcher is null || _dispatcher.IsDisposed || !IsUiThread)
            {
                if (Entries.Count != 0) return false;
                BindDispatcherToCurrentUiThread();
            }
            action();
            return true;
        }

        // Theme animation deliberately uses a process-wide object owner so a
        // rapid day/night reversal replaces the same keyed timeline.  When a
        // WinForms UI sequence is torn down and another one is established in
        // the same process (real restart-in-process tests and handle recreation
        // both exercise this), an otherwise healthy old dispatcher can still
        // reference the ended UI thread.  A caller that is already on a live
        // WinForms message loop is authoritative while no animation is active;
        // bind the global dispatcher to that sequence before posting the object
        // animation.  Never rebind while entries are active.
        if (!IsUiThread && Entries.Count == 0 && Application.MessageLoop &&
            SynchronizationContext.Current is WindowsFormsSynchronizationContext)
        {
            BindDispatcherToCurrentUiThread();
        }

        if (_dispatcher is null)
        {
            if (!Application.MessageLoop && SynchronizationContext.Current is not WindowsFormsSynchronizationContext)
                return false;
            BindDispatcherToCurrentUiThread();
        }

        if (IsUiThread)
        {
            action();
            return true;
        }

        PostToUi(action);
        return true;
    }

    private static void BindDispatcherToCurrentUiThread()
    {
        var previous = _dispatcher;
        if (previous is { IsDisposed: false, IsHandleCreated: true })
        {
            try { previous.BeginInvoke(previous.Dispose); }
            catch (ObjectDisposedException) { }
            catch (InvalidOperationException) { }
        }
        _uiThreadId = Environment.CurrentManagedThreadId;
        _dispatcher = new DispatcherControl();
        _dispatcher.EnsureDispatcherHandle();
        if (_applicationExitHooked) return;
        Application.ApplicationExit += (_, _) =>
        {
            if (IsUiThread) ShutdownOnUi();
            else PostToUi(ShutdownOnUi);
        };
        _applicationExitHooked = true;
    }

    private static void PostToUi(Action action)
    {
        var dispatcher = _dispatcher;
        if (dispatcher is null || dispatcher.IsDisposed || !dispatcher.IsHandleCreated) return;
        try { dispatcher.BeginInvoke(action); }
        catch (InvalidOperationException) when (dispatcher.IsDisposed || !dispatcher.IsHandleCreated) { }
        catch (ObjectDisposedException) { }
    }

    private static Entry? StartCore(
        object owner,
        string key,
        int durationMs,
        Action<double> frame,
        Action? completed,
        Action? cancelled)
    {
        // A same-key reversal replaces the active timeline on the same UI tick.
        // Do not stop/recreate the frame-pump worker between the cancelled leg
        // and its replacement: restarting the waitable-timer phase produced
        // alternating 2-3ms duplicate paints and 12-20ms gaps on a 165Hz panel.
        // Public Cancel still stops the pump when it genuinely becomes idle.
        CancelMatchingEntriesCore(owner, key);
        if (!AnimationTokens.Enabled)
        {
            StopPumpWhenIdle();
            frame(1D);
            completed?.Invoke();
            return null;
        }

        var targetRefreshHz = FramePump.IsRunning
            ? FramePump.CurrentRefreshHz
            : DetectTargetRefreshHz(owner as Control);
        var entry = new Entry
        {
            Owner = owner,
            Key = key,
            StartedAt = Stopwatch.GetTimestamp(),
            DurationMs = durationMs,
            Frame = frame,
            Completed = completed,
            Cancelled = cancelled,
            TargetRefreshHz = targetRefreshHz,
        };
        if (owner is Control paintOwner && owner is not IExplicitAnimationPaintSource)
        {
            entry.PaintHandler = (_, _) => ReportPaint(owner);
            paintOwner.Paint += entry.PaintHandler;
        }
        Entries.Add(entry);
        FramePump.Start(targetRefreshHz);
        // Prime the first invalidation on the UI sequence before the compositor
        // waiter blocks in DwmFlush.  Subsequent frames are compositor paced.
        RequestUiFrameFromWorker();
        return entry;
    }

    private static bool CancelCore(object owner, string? key)
    {
        var removed = CancelMatchingEntriesCore(owner, key);
        StopPumpWhenIdle();
        return removed;
    }

    private static bool CancelMatchingEntriesCore(object owner, string? key)
    {
        var removed = false;
        for (var index = Entries.Count - 1; index >= 0; index--)
        {
            var entry = Entries[index];
            if (!ReferenceEquals(entry.Owner, owner) || key is not null && entry.Key != key) continue;
            Entries.RemoveAt(index);
            CompleteEntry(entry, cancelled: true);
            removed = true;
        }
        return removed;
    }

    private static void CancelExactCore(Entry entry)
    {
        if (!Entries.Remove(entry)) return;
        CompleteEntry(entry, cancelled: true);
        StopPumpWhenIdle();
    }

    private static void CompleteEndpointCore(Entry entry)
    {
        if (!Entries.Remove(entry)) return;
        CompleteEntry(entry, cancelled: false);
        StopPumpWhenIdle();
    }

    private static async Task AwaitAndDisposeAsync(AsyncRunState state)
    {
        try { await state.Completion.Task.ConfigureAwait(false); }
        finally { state.Registration.Dispose(); }
    }

    private static void RequestUiFrameFromWorker()
    {
        if (Interlocked.CompareExchange(ref _uiFramePending, 1, 0) != 0) return;
        var dispatcher = _dispatcher;
        if (dispatcher is null || dispatcher.IsDisposed || !dispatcher.IsHandleCreated)
        {
            Interlocked.Exchange(ref _uiFramePending, 0);
            return;
        }
        try { dispatcher.BeginInvoke(ProcessFrameOnUi); }
        catch (InvalidOperationException) when (dispatcher.IsDisposed || !dispatcher.IsHandleCreated)
        {
            Interlocked.Exchange(ref _uiFramePending, 0);
        }
        catch (ObjectDisposedException)
        {
            Interlocked.Exchange(ref _uiFramePending, 0);
        }
    }

    private static void ProcessFrameOnUi()
    {
        Interlocked.Exchange(ref _uiFramePending, 0);
        if (!IsUiThread) return;
        TickCore();
    }

    private static void TickCore()
    {
        var now = Stopwatch.GetTimestamp();
        for (var index = Entries.Count - 1; index >= 0; index--)
        {
            var entry = Entries[index];
            if (entry.Owner is Control { IsDisposed: true })
            {
                Entries.RemoveAt(index);
                CompleteEntry(entry, cancelled: true);
                continue;
            }

            if (entry.AwaitingEndpointPaint)
            {
                var waitMs = Stopwatch.GetElapsedTime(entry.EndpointRequestedAt, now).TotalMilliseconds;
                var fallbackMs = Math.Min(18D, 1250D / Math.Max(1, entry.TargetRefreshHz));
                if (waitMs < fallbackMs) continue;
                Entries.RemoveAt(index);
                CompleteEntry(entry, cancelled: false);
                continue;
            }

            var elapsedMs = Stopwatch.GetElapsedTime(entry.StartedAt, now).TotalMilliseconds;
            var raw = Math.Clamp(elapsedMs / entry.DurationMs, 0D, 1D);
            if (raw >= 1D)
            {
                entry.AwaitingEndpointPaint = true;
                entry.EndpointRequestedAt = now;
            }
            try { entry.Frame(AnimationTokens.EaseInOut(raw)); }
            catch (ObjectDisposedException) when (entry.Owner is Control)
            {
                Entries.RemoveAt(index);
                CompleteEntry(entry, cancelled: true);
                continue;
            }
            catch (InvalidOperationException) when (entry.Owner is Control control &&
                                                     (control.IsDisposed || !control.IsHandleCreated))
            {
                Entries.RemoveAt(index);
                CompleteEntry(entry, cancelled: true);
                continue;
            }
            if (ConsumeInvalidRegionSynchronouslyForTest && entry.Owner is Control
                { IsDisposed: false, IsHandleCreated: true, Visible: true } paintControl)
            {
                SynchronousTestRefreshCount++;
                paintControl.Refresh();
            }
            if (raw < 1D) continue;
        }
        StopPumpWhenIdle();
    }

    private static void CompleteEntry(Entry entry, bool cancelled)
    {
        if (entry.Owner is Control paintOwner && entry.PaintHandler is not null)
            paintOwner.Paint -= entry.PaintHandler;
        var stats = AnimationPaintStatistics.Create(entry.Key, entry.Owner.GetType().Name,
            entry.TargetRefreshHz, entry.DurationMs, entry.PaintTimestamps,
            entry.EndpointPaintObserved, cancelled);
        CompletedStatistics.Enqueue(stats);
        while (CompletedStatistics.Count > 64) CompletedStatistics.Dequeue();
        AnimationTelemetrySink.TryRecord(stats);
        if (cancelled) entry.Cancelled?.Invoke();
        else entry.Completed?.Invoke();
    }

    private static void StopPumpWhenIdle()
    {
        if (Entries.Count != 0) return;
        FramePump.Stop();
    }

    private static void ShutdownOnUi()
    {
        if (!IsUiThread) return;
        for (var index = Entries.Count - 1; index >= 0; index--)
            CompleteEntry(Entries[index], cancelled: true);
        Entries.Clear();
        FramePump.Stop();
        Interlocked.Exchange(ref _uiFramePending, 0);
        _dispatcher?.Dispose();
        _dispatcher = null;
        _uiThreadId = 0;
    }

    private sealed class AsyncRunState(CancellationToken token)
    {
        public CancellationToken Token { get; } = token;
        public TaskCompletionSource Completion { get; } =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        public CancellationTokenRegistration Registration;
        public volatile bool CancellationRequested;
        public Entry? Entry;
    }
}

public sealed record AnimationPaintStatistics(
    string Key,
    string OwnerType,
    int TargetRefreshHz,
    int DurationMs,
    int PaintFrames,
    double AverageIntervalMs,
    double MedianIntervalMs,
    double P95IntervalMs,
    int DuplicateFrames,
    int MissedRefreshCycles,
    bool EndpointPaintIncluded,
    bool Cancelled,
    double[] PaintIntervalsMs)
{
    internal static AnimationPaintStatistics Create(
        string key,
        string ownerType,
        int targetRefreshHz,
        int durationMs,
        IReadOnlyList<long> paintTimestamps,
        bool endpointPaintIncluded,
        bool cancelled)
    {
        var intervals = paintTimestamps.Zip(paintTimestamps.Skip(1),
            (left, right) => Stopwatch.GetElapsedTime(left, right).TotalMilliseconds)
            .Where(value => value >= 0D)
            .OrderBy(value => value)
            .ToArray();
        var expected = 1000D / Math.Max(1, targetRefreshHz);
        var average = intervals.Length == 0 ? 0D : intervals.Average();
        var median = Percentile(intervals, 0.50D);
        var p95 = Percentile(intervals, 0.95D);
        var duplicate = intervals.Count(value => value < expected * 0.5D);
        var missed = intervals.Sum(value => Math.Max(0, (int)Math.Round(value / expected) - 1));
        return new AnimationPaintStatistics(key, ownerType, targetRefreshHz, durationMs,
            paintTimestamps.Count, average, median, p95, duplicate, missed, endpointPaintIncluded, cancelled,
            intervals.Select(value => Math.Round(value, 4)).ToArray());
    }

    private static double Percentile(IReadOnlyList<double> values, double percentile)
    {
        if (values.Count == 0) return 0D;
        var index = Math.Clamp((int)Math.Ceiling(values.Count * percentile) - 1, 0, values.Count - 1);
        return values[index];
    }
}

internal static class AnimationTelemetrySink
{
    public const string EnvironmentVariableName = "TREASURECHEST_ANIMATION_TELEMETRY_FILE";
    private static readonly SemaphoreSlim WriteGate = new(1, 1);
    public static Exception? LastFailure { get; private set; }
    internal static bool IsEnabled =>
        !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable(EnvironmentVariableName));

    public static void TryRecord(AnimationPaintStatistics statistics)
    {
        var path = Environment.GetEnvironmentVariable(EnvironmentVariableName);
        if (string.IsNullOrWhiteSpace(path)) return;
        var safeKey = statistics.Key.Split(':', 2)[0];
        var line = JsonSerializer.Serialize(new
        {
            captured_at = DateTimeOffset.Now,
            key_category = safeKey,
            statistics.OwnerType,
            statistics.TargetRefreshHz,
            statistics.DurationMs,
            statistics.PaintFrames,
            statistics.AverageIntervalMs,
            statistics.MedianIntervalMs,
            statistics.P95IntervalMs,
            statistics.DuplicateFrames,
            statistics.MissedRefreshCycles,
            statistics.EndpointPaintIncluded,
            statistics.Cancelled,
            statistics.PaintIntervalsMs,
            duplicate_definition = "interval < 0.5 * target refresh period",
            missed_cycle_definition = "max(0, round(interval / target period) - 1)",
        });
        _ = Task.Run(async () =>
        {
            await WriteGate.WaitAsync().ConfigureAwait(false);
            try
            {
                var fullPath = Path.GetFullPath(path);
                Directory.CreateDirectory(Path.GetDirectoryName(fullPath)
                    ?? throw new InvalidOperationException("动画遥测路径缺少父目录。"));
                await File.AppendAllTextAsync(fullPath, line + Environment.NewLine).ConfigureAwait(false);
            }
            catch (Exception error) { LastFailure = error; }
            finally { WriteGate.Release(); }
        });
    }
}

internal static class ThemeConsumerPaintTelemetry
{
    internal sealed record PaintBreakdown(string Key, string OwnerType, int TotalPaints, int IntermediatePaints);

    private sealed class Observation(Control control, string key, int refreshHz)
    {
        public Control Control { get; } = control;
        public string Key { get; } = key;
        public int RefreshHz { get; } = refreshHz;
        public List<long> Timestamps { get; } = [];
        public int IntermediatePaints { get; set; }
        public PaintEventHandler? Handler { get; set; }
    }

    private static readonly List<Observation> Active = [];
    private static readonly List<AnimationPaintStatistics> Completed = [];
    private static readonly List<PaintBreakdown> CompletedBreakdown = [];
    private static int _durationMs;
    internal static bool CaptureForTests { get; set; }
    internal static IReadOnlyList<AnimationPaintStatistics> LastCompleted => Completed;
    internal static IReadOnlyList<PaintBreakdown> LastCompletedBreakdown => CompletedBreakdown;

    public static void Begin(IEnumerable<Form> forms, int durationMs)
    {
        Complete(cancelled: true, endpointPaintIncluded: false);
        if (!CaptureForTests && !AnimationTelemetrySink.IsEnabled) return;
        _durationMs = durationMs;
        var controls = new List<Control>();
        foreach (var form in forms.Where(form => form.Visible && form.IsHandleCreated && !form.IsDisposed))
        {
            controls.Add(form);
            controls.AddRange(Walk(form).Where(IsVisibleThemeConsumer));
        }

        var ordinalByType = new Dictionary<string, int>(StringComparer.Ordinal);
        var uniqueControls = new HashSet<Control>(ReferenceEqualityComparer.Instance);
        foreach (var control in controls)
        {
            if (!uniqueControls.Add(control)) continue;
            var type = control.GetType().Name;
            ordinalByType.TryGetValue(type, out var ordinal);
            ordinalByType[type] = ordinal + 1;
            var observation = new Observation(control, $"palette-consumer:{type}:{ordinal}",
                NativeRefreshRate.Detect(control));
            observation.Handler = (_, _) =>
            {
                observation.Timestamps.Add(Stopwatch.GetTimestamp());
                if (UiTheme.IsTransitioning) observation.IntermediatePaints++;
            };
            control.Paint += observation.Handler;
            Active.Add(observation);
        }
    }

    public static void Complete(bool cancelled, bool endpointPaintIncluded)
    {
        if (Active.Count == 0) return;
        foreach (var observation in Active)
        {
            if (observation.Handler is not null && !observation.Control.IsDisposed)
                observation.Control.Paint -= observation.Handler;
            var statistics = AnimationPaintStatistics.Create(
                observation.Key,
                observation.Control.GetType().Name,
                observation.RefreshHz,
                _durationMs,
                observation.Timestamps,
                endpointPaintIncluded && observation.Timestamps.Count > 0,
                cancelled);
            Completed.Add(statistics);
            CompletedBreakdown.Add(new PaintBreakdown(observation.Key,
                observation.Control.GetType().Name, observation.Timestamps.Count,
                observation.IntermediatePaints));
            AnimationTelemetrySink.TryRecord(statistics);
        }
        Active.Clear();
        if (Completed.Count > 64) Completed.RemoveRange(0, Completed.Count - 64);
        if (CompletedBreakdown.Count > 64)
            CompletedBreakdown.RemoveRange(0, CompletedBreakdown.Count - 64);
    }

    internal static void Report(object owner, long timestamp)
    {
        foreach (var observation in Active)
        {
            if (!ReferenceEquals(observation.Control, owner)) continue;
            if (observation.Timestamps.Count > 0 &&
                Stopwatch.GetElapsedTime(observation.Timestamps[^1], timestamp).TotalMilliseconds < 1D)
                observation.Timestamps[^1] = timestamp;
            else
            {
                observation.Timestamps.Add(timestamp);
                if (UiTheme.IsTransitioning) observation.IntermediatePaints++;
            }
        }
    }

    internal static void Reset()
    {
        Complete(cancelled: true, endpointPaintIncluded: false);
        Completed.Clear();
        CompletedBreakdown.Clear();
    }

    internal static bool IsVisibleThemeConsumer(Control control)
    {
        if (!control.Visible || !control.IsHandleCreated || control.IsDisposed ||
            control.ClientSize.Width <= 0 || control.ClientSize.Height <= 0)
            return false;
        var consumesTheme = control switch
        {
            IExplicitAnimationPaintSource source => source.IncludeInThemePaintTelemetry,
            TextBoxBase or ComboBox or ListBox or UpDownBase or ScrollBar => false,
            Label or Panel or GroupBox or PictureBox => true,
            _ => false,
        };
        if (!consumesTheme) return false;
        try
        {
            var visible = control.RectangleToScreen(control.ClientRectangle);
            for (Control? ancestor = control.Parent; ancestor is not null; ancestor = ancestor.Parent)
            {
                if (!ancestor.Visible || ancestor.ClientSize.Width <= 0 || ancestor.ClientSize.Height <= 0)
                    return false;
                visible = Rectangle.Intersect(visible, ancestor.RectangleToScreen(ancestor.ClientRectangle));
                if (visible.Width <= 0 || visible.Height <= 0) return false;
            }
            return true;
        }
        catch (InvalidOperationException)
        {
            return false;
        }
    }

    private static IEnumerable<Control> Walk(Control parent)
    {
        foreach (Control child in parent.Controls)
        {
            yield return child;
            foreach (var descendant in Walk(child)) yield return descendant;
        }
    }
}

internal sealed class CompositionFramePump(Action requestUiFrame)
{
    private sealed class WorkerState(int generation, int refreshHz)
    {
        public int Generation { get; } = generation;
        public int RefreshHz { get; } = refreshHz;
        public CancellationTokenSource Cancellation { get; } = new();
        public Task? Task { get; set; }
    }

    private readonly object _gate = new();
    private WorkerState? _active;
    private int _generation;
    private int _workerCount;
    private int _outstandingCancellationSources;

    public int CurrentRefreshHz { get; private set; } = 60;
    public int WorkerCount => Volatile.Read(ref _workerCount);
    public int OutstandingCancellationSources => Volatile.Read(ref _outstandingCancellationSources);
    public Exception? LastWorkerFailure { get; private set; }

    public bool IsRunning
    {
        get
        {
            lock (_gate) return _active is { Cancellation.IsCancellationRequested: false };
        }
    }

    public void Start(int refreshHz)
    {
        var normalized = AnimationRunner.NormalizeTargetRefreshHz(refreshHz);
        lock (_gate)
        {
            if (_active is { Cancellation.IsCancellationRequested: false }) return;
            CurrentRefreshHz = normalized;
            var state = new WorkerState(++_generation, normalized);
            Interlocked.Increment(ref _outstandingCancellationSources);
            _active = state;
            state.Task = Task.Factory.StartNew(() => RunAndDispose(state), CancellationToken.None,
                TaskCreationOptions.LongRunning, TaskScheduler.Default);
        }
    }

    public void Stop()
    {
        lock (_gate)
        {
            _active?.Cancellation.Cancel();
            _active = null;
        }
    }

    private void RunAndDispose(WorkerState state)
    {
        Interlocked.Increment(ref _workerCount);
        try
        {
            Run(state.Cancellation.Token, state.RefreshHz);
        }
        catch (OperationCanceledException) when (state.Cancellation.IsCancellationRequested) { }
        catch (Exception error)
        {
            LastWorkerFailure = error;
        }
        finally
        {
            // The token wait handle is owned by this worker.  Dispose only after
            // HighResolutionFrameWaiter and any in-flight DwmFlush have returned.
            state.Cancellation.Dispose();
            Interlocked.Decrement(ref _outstandingCancellationSources);
            Interlocked.Decrement(ref _workerCount);
            lock (_gate)
            {
                if (ReferenceEquals(_active, state)) _active = null;
            }
        }
    }

    private void Run(CancellationToken cancellationToken, int refreshHz)
    {
        using var sleeper = new HighResolutionFrameWaiter(cancellationToken);
        var fallbackPeriod = TimeSpan.FromSeconds(1D / refreshHz);
        // DwmFlush is a useful clock at conventional compositor rates, but on
        // high-refresh per-monitor displays it can still block on a 60/66-Hz
        // desktop compositor (and some drivers impose a much longer first
        // flush).  The owner display rate was resolved at this idle->active
        // boundary, so 120/144/165-Hz runs use the high-resolution kernel timer
        // at that exact rate from their first frame instead of gambling the
        // first visible interval on an unbounded DwmFlush probe.
        var useCompositionClock = ShouldUseCompositionClock(
            refreshHz,
            SystemInformation.TerminalServerSession,
            NativeRefreshRate.IsCompositionEnabled());
        while (!cancellationToken.IsCancellationRequested)
        {
            var waitedForFrame = false;
            if (useCompositionClock)
            {
                var started = Stopwatch.GetTimestamp();
                var synchronized = NativeRefreshRate.TryFlushComposition();
                var elapsedMs = Stopwatch.GetElapsedTime(started).TotalMilliseconds;
                // Some drivers report success without blocking.  Treat that as an
                // unavailable compositor clock so the loop cannot busy-spin.
                waitedForFrame = synchronized && elapsedMs >= 1D;
                // DwmFlush can legitimately follow a 60/66-Hz desktop compositor
                // even when the owner is on a 120/144/165-Hz monitor.  Keeping that
                // clock would silently cap real WinForms Paint delivery near 60fps.
                // After one measured mismatch, keep the current waited frame and use
                // the high-resolution waitable timer for the rest of this active run.
                if (!synchronized || !CompositionCadenceMatchesTarget(elapsedMs, refreshHz))
                    useCompositionClock = false;
            }
            if (!waitedForFrame && !sleeper.Wait(fallbackPeriod)) break;
            if (cancellationToken.IsCancellationRequested) break;
            requestUiFrame();
        }
    }

    internal static bool ShouldUseCompositionClock(int refreshHz, bool terminalSession, bool compositionEnabled) =>
        compositionEnabled && !terminalSession && AnimationRunner.NormalizeTargetRefreshHz(refreshHz) <= 75;

    internal static bool CompositionCadenceMatchesTarget(double elapsedMs, int refreshHz)
    {
        if (elapsedMs < 1D) return false;
        var targetPeriodMs = 1000D / AnimationRunner.NormalizeTargetRefreshHz(refreshHz);
        return elapsedMs <= targetPeriodMs * 1.75D;
    }
}

internal static class NativeRefreshRate
{
    private const int CurrentSettings = -1;
    private const int VertRefresh = 116;
    internal static Func<Control?, int?>? TestReader { get; set; }
    internal static int ReadCount { get; private set; }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern IntPtr GetDC(IntPtr window);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern int ReleaseDC(IntPtr window, IntPtr dc);
    [System.Runtime.InteropServices.DllImport("gdi32.dll")]
    private static extern int GetDeviceCaps(IntPtr dc, int index);
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool EnumDisplaySettings(string? deviceName, int modeNumber, ref DevMode deviceMode);

    public static int Detect(Control? owner)
    {
        ReadCount++;
        if (TestReader is not null)
            return AnimationRunner.NormalizeTargetRefreshHz(TestReader(owner) ?? 60);
        var deviceName = owner is { IsDisposed: false } ? Screen.FromControl(owner).DeviceName : null;
        return ResolveReportedRefreshHz(TryReadDevice(deviceName), TryReadPrimaryDesktop());
    }

    internal static int ResolveReportedRefreshHz(int? displayValue, int? desktopValue) =>
        AnimationRunner.NormalizeTargetRefreshHz(displayValue ?? desktopValue ?? 60);

    private static int? TryReadDevice(string? deviceName)
    {
        if (string.IsNullOrWhiteSpace(deviceName)) return null;
        var mode = new DevMode { Size = (ushort)Marshal.SizeOf<DevMode>() };
        if (!EnumDisplaySettings(deviceName, CurrentSettings, ref mode)) return null;
        return mode.DisplayFrequency > 1 ? (int)mode.DisplayFrequency : null;
    }

    private static int? TryReadPrimaryDesktop()
    {
        var dc = GetDC(IntPtr.Zero);
        if (dc == IntPtr.Zero) return null;
        try
        {
            var value = GetDeviceCaps(dc, VertRefresh);
            return value > 1 ? value : null;
        }
        finally { ReleaseDC(IntPtr.Zero, dc); }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct DevMode
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        public ushort SpecVersion;
        public ushort DriverVersion;
        public ushort Size;
        public ushort DriverExtra;
        public uint Fields;
        public int PositionX;
        public int PositionY;
        public uint DisplayOrientation;
        public uint DisplayFixedOutput;
        public short Color;
        public short Duplex;
        public short YResolution;
        public short TTOption;
        public short Collate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string FormName;
        public ushort LogPixels;
        public uint BitsPerPixel;
        public uint PixelsWidth;
        public uint PixelsHeight;
        public uint DisplayFlags;
        public uint DisplayFrequency;
        public uint IcmMethod;
        public uint IcmIntent;
        public uint MediaType;
        public uint DitherType;
        public uint Reserved1;
        public uint Reserved2;
        public uint PanningWidth;
        public uint PanningHeight;
    }

    public static bool IsCompositionEnabled()
    {
        try { return NativeMethods.DwmIsCompositionEnabled(out var enabled) == 0 && enabled; }
        catch (DllNotFoundException) { return false; }
        catch (EntryPointNotFoundException) { return false; }
        catch (BadImageFormatException) { return false; }
    }

    public static bool TryFlushComposition()
    {
        try { return NativeMethods.DwmFlush() == 0; }
        catch (DllNotFoundException) { return false; }
        catch (EntryPointNotFoundException) { return false; }
        catch (BadImageFormatException) { return false; }
    }
}

internal sealed class HighResolutionFrameWaiter : IDisposable
{
    private static int _activeNativeTimerCount;
    private static int _activeCancellationWaitHandleCount;
    private readonly CancellationToken _cancellationToken;
    private readonly WaitHandle _cancellationWaitHandle;
    private readonly IntPtr _timer;
    private readonly IntPtr[] _waitHandles;

    public static int ActiveNativeTimerCount => Volatile.Read(ref _activeNativeTimerCount);
    public static int ActiveCancellationWaitHandleCount => Volatile.Read(ref _activeCancellationWaitHandleCount);

    public HighResolutionFrameWaiter(CancellationToken cancellationToken)
    {
        _cancellationToken = cancellationToken;
        _cancellationWaitHandle = cancellationToken.WaitHandle;
        Interlocked.Increment(ref _activeCancellationWaitHandleCount);
        _timer = NativeMethods.CreateWaitableTimerEx(IntPtr.Zero, null,
            NativeMethods.CreateWaitableTimerHighResolution, NativeMethods.TimerAllAccess);
        if (_timer == IntPtr.Zero)
        {
            _waitHandles = [];
            return;
        }
        Interlocked.Increment(ref _activeNativeTimerCount);
        _waitHandles = [_timer, _cancellationWaitHandle.SafeWaitHandle.DangerousGetHandle()];
    }

    public bool Wait(TimeSpan interval)
    {
        if (_cancellationToken.IsCancellationRequested) return false;
        if (_timer == IntPtr.Zero)
            return !_cancellationWaitHandle.WaitOne(interval);

        var dueTime = -Math.Max(1L, (long)Math.Round(interval.TotalMilliseconds * 10_000D));
        if (!NativeMethods.SetWaitableTimer(_timer, ref dueTime, 0, IntPtr.Zero, IntPtr.Zero, false))
            return !_cancellationWaitHandle.WaitOne(interval);
        var result = NativeMethods.WaitForMultipleObjects((uint)_waitHandles.Length,
            _waitHandles, false, (uint)Math.Ceiling(interval.TotalMilliseconds * 4D + 20D));
        return result == NativeMethods.WaitObject0;
    }

    public void Dispose()
    {
        if (_timer != IntPtr.Zero)
        {
            NativeMethods.CancelWaitableTimer(_timer);
            NativeMethods.CloseHandle(_timer);
            Interlocked.Decrement(ref _activeNativeTimerCount);
        }
        Interlocked.Decrement(ref _activeCancellationWaitHandleCount);
    }
}
