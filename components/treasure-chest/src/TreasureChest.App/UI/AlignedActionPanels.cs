namespace TreasureChest.UI;

internal sealed class AlignedButtonStackPanel : Panel, IManualDpiLayoutParticipant
{
    private const int LogicalButtonWidth = 90;
    private const int LogicalButtonHeight = 42;
    private const int LogicalGap = 8;
    private readonly Button _upper;
    private readonly Button _lower;

    public AlignedButtonStackPanel(Button upper, Button lower)
    {
        _upper = upper;
        _lower = lower;
        BackColor = UiTheme.Background;
        Controls.AddRange([upper, lower]);
    }

    internal (Rectangle Upper, Rectangle Lower) ButtonBoundsSnapshot => (_upper.Bounds, _lower.Bounds);
    private int _layoutDpi = DpiLayout.BaselineDpi;
    public int LayoutDpi
    {
        get => _layoutDpi;
        set { _layoutDpi = DpiLayout.NormalizeDpi(value); PerformLayout(); }
    }

    protected override void OnLayout(LayoutEventArgs levent)
    {
        base.OnLayout(levent);
        var dpi = LayoutDpi;
        var buttonWidth = Math.Min(ClientSize.Width, DpiLayout.Scale(LogicalButtonWidth, dpi));
        var buttonHeight = DpiLayout.Scale(LogicalButtonHeight, dpi);
        var gap = DpiLayout.Scale(LogicalGap, dpi);
        var totalHeight = buttonHeight * 2 + gap;
        var left = Math.Max(0, (ClientSize.Width - buttonWidth) / 2);
        var top = Math.Max(0, (ClientSize.Height - totalHeight) / 2);
        _upper.Bounds = new Rectangle(left, top, buttonWidth, buttonHeight);
        _lower.Bounds = new Rectangle(left, top + buttonHeight + gap, buttonWidth, buttonHeight);
    }
}

internal sealed class SectionActionHeaderPanel : Panel, IManualDpiLayoutParticipant
{
    private readonly Label _title;
    private readonly Button? _action;

    public SectionActionHeaderPanel(Label title, Button? action = null)
    {
        _title = title;
        _action = action;
        BackColor = UiTheme.Surface;
        Controls.Add(title);
        if (action is not null) Controls.Add(action);
    }

    internal (Rectangle Title, Rectangle? Action) ItemBoundsSnapshot => (_title.Bounds, _action?.Bounds);
    private int _layoutDpi = DpiLayout.BaselineDpi;
    public int LayoutDpi
    {
        get => _layoutDpi;
        set { _layoutDpi = DpiLayout.NormalizeDpi(value); PerformLayout(); }
    }

    protected override void OnLayout(LayoutEventArgs levent)
    {
        base.OnLayout(levent);
        var dpi = LayoutDpi;
        var inset = DpiLayout.Scale(4, dpi);
        if (_action is null)
        {
            _title.Bounds = ClientRectangle;
            return;
        }

        var preferred = _action.GetPreferredSize(Size.Empty);
        var width = Math.Min(ClientSize.Width, Math.Max(DpiLayout.Scale(160, dpi), preferred.Width + DpiLayout.Scale(6, dpi)));
        var height = Math.Min(ClientSize.Height, Math.Max(DpiLayout.Scale(40, dpi), preferred.Height));
        var actionBounds = new Rectangle(
            Math.Max(0, ClientSize.Width - width - inset),
            Math.Max(0, (ClientSize.Height - height) / 2),
            width,
            height);
        _action.Bounds = actionBounds;
        _title.Bounds = new Rectangle(0, 0, Math.Max(1, actionBounds.Left - inset), ClientSize.Height);
    }
}

internal sealed class ManagerPageHeaderPanel : Panel, IManualDpiLayoutParticipant
{
    internal readonly record struct HeaderTextLayout(Rectangle Title, Rectangle Description);

    public Label PageTitle { get; }
    public Label Description { get; }

    public ManagerPageHeaderPanel(string title, string description)
    {
        BackColor = UiTheme.Background;
        PageTitle = new Label
        {
            Text = title,
            ForeColor = UiTheme.Text,
            Font = ApprovedMonitorManagerLayout.CreatePageTitleFont(),
            TextAlign = ContentAlignment.MiddleLeft,
            AutoEllipsis = false,
        };
        Description = new Label
        {
            Text = description,
            ForeColor = UiTheme.Muted,
            Font = ApprovedMonitorManagerLayout.CreateBodyFont(),
            TextAlign = ContentAlignment.MiddleLeft,
            AutoEllipsis = false,
        };
        Controls.AddRange([PageTitle, Description]);
    }

    internal (Rectangle Title, Rectangle Description) TextBoundsSnapshot => (PageTitle.Bounds, Description.Bounds);
    private int _layoutDpi = DpiLayout.BaselineDpi;
    public int LayoutDpi
    {
        get => _layoutDpi;
        set { _layoutDpi = DpiLayout.NormalizeDpi(value); PerformLayout(); }
    }

    internal static HeaderTextLayout CalculateLayout(
        Size clientSize,
        int dpi,
        Size titleInk,
        Size descriptionInk)
    {
        var horizontal = DpiLayout.Scale(24, dpi);
        var safety = DpiLayout.Scale(3, dpi);
        var gap = DpiLayout.Scale(1, dpi);
        var availableWidth = Math.Max(1, clientSize.Width - horizontal * 2);
        var titleHeight = titleInk.Height + safety * 2;
        var descriptionHeight = descriptionInk.Height + safety * 2;
        var totalHeight = titleHeight + gap + descriptionHeight;
        var top = Math.Max(0, (clientSize.Height - totalHeight) / 2);
        return new HeaderTextLayout(
            new Rectangle(horizontal, top, availableWidth, titleHeight),
            new Rectangle(horizontal, top + titleHeight + gap, availableWidth, descriptionHeight));
    }

    protected override void OnLayout(LayoutEventArgs levent)
    {
        base.OnLayout(levent);
        var dpi = LayoutDpi;
        var availableWidth = Math.Max(1, ClientSize.Width - DpiLayout.Scale(48, dpi));
        var titleInk = NativeButtonText.MeasureAtDpi(PageTitle.Text, PageTitle.Font, dpi,
            new Size(availableWidth, int.MaxValue), TextFormatFlags.NoPadding | TextFormatFlags.SingleLine);
        var descriptionInk = NativeButtonText.MeasureAtDpi(Description.Text, Description.Font, dpi,
            new Size(availableWidth, int.MaxValue), TextFormatFlags.NoPadding | TextFormatFlags.SingleLine);
        var layout = CalculateLayout(ClientSize, dpi, titleInk, descriptionInk);
        PageTitle.Bounds = layout.Title;
        Description.Bounds = layout.Description;
    }
}
