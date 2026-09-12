namespace TreasureChest.UI;

public enum ThemeMode
{
    Day,
    Night,
}

public sealed record ThemePalette(
    Color Background,
    Color Surface,
    Color SurfaceAlt,
    Color SurfaceHover,
    Color TitleBar,
    Color Navigation,
    Color NavigationHover,
    Color NavigationPressed,
    Color NavigationText,
    Color NavigationMuted,
    Color Accent,
    Color AccentHover,
    Color AccentPressed,
    Color AccentSoft,
    Color AccentSoftStrong,
    Color OnAccent,
    Color Text,
    Color Muted,
    Color Border,
    Color GridWellBorder,
    Color GridLine,
    Color DrawerSelected,
    Color Divider,
    Color Running,
    Color Stopped,
    Color Warning,
    Color WarningSurface,
    Color DisabledBackground,
    Color DisabledText,
    Color CloseHover,
    Color ClosePressed,
    Color ProgressTrack)
{
    public static ThemePalette Day { get; } = new(
        Color.FromArgb(241, 240, 249), Color.FromArgb(253, 252, 255), Color.FromArgb(247, 247, 251),
        Color.FromArgb(246, 244, 252), Color.FromArgb(241, 241, 249), Color.FromArgb(238, 237, 248),
        Color.FromArgb(243, 242, 251), Color.FromArgb(233, 232, 247), Color.FromArgb(26, 31, 47),
        Color.FromArgb(91, 94, 111), Color.FromArgb(101, 76, 244), Color.FromArgb(87, 62, 226),
        Color.FromArgb(74, 50, 207), Color.FromArgb(234, 230, 255), Color.FromArgb(218, 211, 255),
        Color.White, Color.FromArgb(24, 29, 45), Color.FromArgb(96, 99, 116), Color.FromArgb(228, 229, 235),
        Color.FromArgb(236, 236, 244), Color.FromArgb(238, 239, 244), Color.FromArgb(236, 236, 236), Color.FromArgb(196, 196, 210),
        Color.FromArgb(22, 125, 78), Color.FromArgb(170, 50, 74), Color.FromArgb(155, 72, 50),
        Color.FromArgb(252, 235, 229), Color.FromArgb(236, 235, 241), Color.FromArgb(146, 145, 155),
        Color.FromArgb(194, 63, 86), Color.FromArgb(167, 49, 73), Color.FromArgb(225, 225, 234));

    public static ThemePalette Night { get; } = new(
        Color.FromArgb(24, 28, 35), Color.FromArgb(32, 36, 45), Color.FromArgb(29, 33, 42),
        Color.FromArgb(38, 43, 54), Color.FromArgb(24, 28, 35), Color.FromArgb(32, 36, 45),
        Color.FromArgb(36, 40, 50), Color.FromArgb(36, 39, 51), Color.FromArgb(244, 244, 248),
        Color.FromArgb(190, 191, 202), Color.FromArgb(111, 88, 255), Color.FromArgb(125, 103, 255),
        Color.FromArgb(93, 69, 231), Color.FromArgb(52, 47, 82), Color.FromArgb(66, 58, 105),
        Color.White, Color.FromArgb(243, 243, 247), Color.FromArgb(177, 178, 190), Color.FromArgb(57, 62, 73),
        Color.FromArgb(49, 54, 65), Color.FromArgb(52, 57, 68), Color.FromArgb(54, 58, 68), Color.FromArgb(74, 79, 94),
        Color.FromArgb(56, 203, 125), Color.FromArgb(236, 100, 121), Color.FromArgb(229, 137, 99),
        Color.FromArgb(73, 48, 45), Color.FromArgb(45, 49, 58), Color.FromArgb(126, 129, 141),
        Color.FromArgb(210, 70, 94), Color.FromArgb(184, 55, 80), Color.FromArgb(48, 53, 64));

    // A straight day/night interpolation makes dark-on-light and
    // light-on-dark foreground roles meet their surfaces at the midpoint.
    // At that instant only the anti-aliased outline remains visible.  Route
    // animation colours through one contrast-safe chromatic palette instead;
    // stable day/night endpoints still use the native palettes above.
    internal static ThemePalette AnimationBridge { get; } = new(
        Color.FromArgb(145, 147, 155), Color.FromArgb(145, 147, 155), Color.FromArgb(145, 147, 155),
        Color.FromArgb(145, 147, 155), Color.FromArgb(145, 147, 155), Color.FromArgb(145, 147, 155),
        Color.FromArgb(145, 147, 155), Color.FromArgb(145, 147, 155), Color.FromArgb(65, 67, 190),
        Color.FromArgb(88, 90, 190), Color.FromArgb(106, 82, 250), Color.FromArgb(106, 82, 244),
        Color.FromArgb(84, 60, 219), Color.FromArgb(143, 139, 169), Color.FromArgb(142, 134, 180),
        Color.White, Color.FromArgb(70, 72, 200), Color.FromArgb(88, 90, 195), Color.FromArgb(104, 107, 119),
        Color.FromArgb(98, 102, 115), Color.FromArgb(108, 111, 124), Color.FromArgb(132, 122, 177),
        Color.FromArgb(108, 111, 124), Color.FromArgb(39, 164, 101), Color.FromArgb(203, 72, 98),
        Color.FromArgb(192, 104, 70), Color.FromArgb(157, 112, 107), Color.FromArgb(116, 118, 128),
        Color.FromArgb(92, 94, 175), Color.FromArgb(202, 66, 90), Color.FromArgb(176, 52, 77),
        Color.FromArgb(108, 111, 124));

    public IReadOnlyList<Color> Colors =>
    [
        Background, Surface, SurfaceAlt, SurfaceHover, TitleBar, Navigation, NavigationHover, NavigationPressed,
        NavigationText, NavigationMuted, Accent, AccentHover, AccentPressed, AccentSoft, AccentSoftStrong, OnAccent,
        Text, Muted, Border, GridWellBorder, GridLine, DrawerSelected, Divider, Running, Stopped, Warning, WarningSurface,
        DisabledBackground, DisabledText, CloseHover, ClosePressed, ProgressTrack,
    ];

    public static ThemePalette Interpolate(ThemePalette from, ThemePalette to, double progress)
    {
        static Color Lerp(Color a, Color b, double t) => Color.FromArgb(
            (int)Math.Round(a.A + (b.A - a.A) * t),
            (int)Math.Round(a.R + (b.R - a.R) * t),
            (int)Math.Round(a.G + (b.G - a.G) * t),
            (int)Math.Round(a.B + (b.B - a.B) * t));
        var left = from.Colors;
        var right = to.Colors;
        var c = new Color[left.Count];
        for (var index = 0; index < c.Length; index++) c[index] = Lerp(left[index], right[index], progress);
        return new ThemePalette(c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], c[9], c[10], c[11],
            c[12], c[13], c[14], c[15], c[16], c[17], c[18], c[19], c[20], c[21], c[22], c[23], c[24],
            c[25], c[26], c[27], c[28], c[29], c[30], c[31]);
    }

    internal static ThemePalette InterpolateForAnimation(ThemePalette from, ThemePalette to, double progress)
    {
        progress = Math.Clamp(progress, 0D, 1D);
        if (progress <= 0D) return from;
        if (progress >= 1D) return to;
        return progress <= 0.5D
            ? Interpolate(from, AnimationBridge, progress * 2D)
            : Interpolate(AnimationBridge, to, (progress - 0.5D) * 2D);
    }

}
