using System.Buffers.Binary;
using System.ComponentModel;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

internal static class Program
{
    private static readonly int[] RequiredSizes = [16, 20, 24, 32, 40, 48, 64, 128, 256];
    private const uint LoadLibraryAsDataFile = 0x00000002;
    private const uint LoadLibraryAsImageResource = 0x00000020;
    private static readonly IntPtr RtGroupIcon = (IntPtr)14;

    private static int Main(string[] args)
    {
        if (args is ["--verify-exe", var sourcePath, var iconPath, var executablePath])
        {
            VerifySource(sourcePath);
            VerifyIcon(iconPath);
            VerifyExecutableIcon(executablePath);
            Console.WriteLine($"Verified source, ICO and executable icon: {string.Join(", ", RequiredSizes)} px");
            return 0;
        }

        if (args is not [var source, var target])
            throw new ArgumentException("用法：IconGenerator <源 PNG> <目标 ICO> | --verify-exe <源 PNG> <ICO> <EXE>");

        GenerateIcon(source, target);
        VerifyIcon(target);
        Console.WriteLine($"Generated and verified icon: {string.Join(", ", RequiredSizes)} px");
        return 0;
    }

    private static void GenerateIcon(string sourcePath, string targetPath)
    {
        VerifySource(sourcePath);
        using var source = Image.FromFile(sourcePath);
        var payloads = new List<byte[]>(RequiredSizes.Length);
        foreach (var size in RequiredSizes)
        {
            using var bitmap = new Bitmap(size, size, PixelFormat.Format32bppArgb);
            bitmap.SetResolution(96, 96);
            using (var graphics = Graphics.FromImage(bitmap))
            using (var attributes = new ImageAttributes())
            {
                graphics.CompositingMode = CompositingMode.SourceCopy;
                graphics.CompositingQuality = CompositingQuality.HighQuality;
                graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
                graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
                graphics.SmoothingMode = SmoothingMode.HighQuality;
                attributes.SetWrapMode(WrapMode.TileFlipXY);
                graphics.DrawImage(source, new Rectangle(0, 0, size, size), 0, 0,
                    source.Width, source.Height, GraphicsUnit.Pixel, attributes);
            }
            using var stream = new MemoryStream();
            bitmap.Save(stream, ImageFormat.Png);
            payloads.Add(stream.ToArray());
        }

        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(targetPath))!);
        using var output = new BinaryWriter(File.Create(targetPath));
        output.Write((ushort)0);
        output.Write((ushort)1);
        output.Write((ushort)RequiredSizes.Length);
        var offset = 6 + RequiredSizes.Length * 16;
        for (var index = 0; index < RequiredSizes.Length; index++)
        {
            var size = RequiredSizes[index];
            output.Write((byte)(size == 256 ? 0 : size));
            output.Write((byte)(size == 256 ? 0 : size));
            output.Write((byte)0);
            output.Write((byte)0);
            output.Write((ushort)1);
            output.Write((ushort)32);
            output.Write(payloads[index].Length);
            output.Write(offset);
            offset += payloads[index].Length;
        }
        foreach (var payload in payloads) output.Write(payload);
    }

    private static void VerifySource(string sourcePath)
    {
        if (!File.Exists(sourcePath)) throw new FileNotFoundException("源 PNG 不存在。", sourcePath);
        using var source = Image.FromFile(sourcePath);
        if (!source.RawFormat.Equals(ImageFormat.Png)) throw new InvalidDataException("应用图标源文件必须是 PNG。");
        if (source.Width != source.Height || source.Width < 256)
            throw new InvalidDataException("应用图标源文件必须是至少 256 像素的正方形原图。");
    }

    private static void VerifyIcon(string iconPath)
    {
        var bytes = File.ReadAllBytes(iconPath);
        var entries = ReadIconDirectory(bytes, groupResource: false);
        AssertSizes(entries.Select(entry => entry.Size), $"ICO {iconPath}");
        foreach (var entry in entries)
        {
            if (entry.BitCount != 32) throw new InvalidDataException($"{entry.Size}px 图层不是 32 位颜色。");
            var payload = bytes.AsSpan(entry.Offset, entry.BytesInResource);
            if (payload.Length < 24 || !payload[..8].SequenceEqual(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 }))
                throw new InvalidDataException($"{entry.Size}px 图层不是 PNG 压缩图层。");
            var width = BinaryPrimitives.ReadInt32BigEndian(payload.Slice(16, 4));
            var height = BinaryPrimitives.ReadInt32BigEndian(payload.Slice(20, 4));
            if (width != entry.Size || height != entry.Size)
                throw new InvalidDataException($"{entry.Size}px 图层的 PNG 尺寸为 {width}x{height}。");
        }
    }

    private static void VerifyExecutableIcon(string executablePath)
    {
        if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException("EXE 图标资源验证仅支持 Windows。");
        var module = LoadLibraryEx(executablePath, IntPtr.Zero, LoadLibraryAsDataFile | LoadLibraryAsImageResource);
        if (module == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "无法只读加载 EXE 资源。");
        try
        {
            var names = new List<IntPtr>();
            EnumResNameProc callback = (_, _, name, _) =>
            {
                names.Add(name);
                return true;
            };
            if (!EnumResourceNames(module, RtGroupIcon, callback, IntPtr.Zero) && names.Count == 0)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "EXE 中没有可枚举的 RT_GROUP_ICON。");

            var verified = false;
            foreach (var name in names)
            {
                var resource = FindResource(module, name, RtGroupIcon);
                if (resource == IntPtr.Zero) continue;
                var size = SizeofResource(module, resource);
                var loaded = LoadResource(module, resource);
                var pointer = loaded == IntPtr.Zero ? IntPtr.Zero : LockResource(loaded);
                if (pointer == IntPtr.Zero || size == 0) continue;
                var bytes = new byte[size];
                Marshal.Copy(pointer, bytes, 0, bytes.Length);
                var entries = ReadIconDirectory(bytes, groupResource: true);
                if (entries.Select(entry => entry.Size).SequenceEqual(RequiredSizes))
                {
                    verified = true;
                    break;
                }
            }
            if (!verified)
                throw new InvalidDataException($"EXE 未嵌入完整应用图标层：{string.Join(", ", RequiredSizes)}。");
        }
        finally
        {
            FreeLibrary(module);
        }
    }

    private static IReadOnlyList<IconEntry> ReadIconDirectory(ReadOnlySpan<byte> bytes, bool groupResource)
    {
        if (bytes.Length < 6 || BinaryPrimitives.ReadUInt16LittleEndian(bytes[..2]) != 0 ||
            BinaryPrimitives.ReadUInt16LittleEndian(bytes.Slice(2, 2)) != 1)
            throw new InvalidDataException("图标目录头无效。");
        var count = BinaryPrimitives.ReadUInt16LittleEndian(bytes.Slice(4, 2));
        var entryLength = groupResource ? 14 : 16;
        if (count == 0 || bytes.Length < 6 + count * entryLength) throw new InvalidDataException("图标目录不完整。");
        var result = new List<IconEntry>(count);
        for (var index = 0; index < count; index++)
        {
            var entry = bytes.Slice(6 + index * entryLength, entryLength);
            var width = entry[0] == 0 ? 256 : entry[0];
            var height = entry[1] == 0 ? 256 : entry[1];
            if (width != height) throw new InvalidDataException($"图标层不是正方形：{width}x{height}。");
            var bitCount = BinaryPrimitives.ReadUInt16LittleEndian(entry.Slice(6, 2));
            var bytesInResource = BinaryPrimitives.ReadInt32LittleEndian(entry.Slice(8, 4));
            var offset = groupResource ? 0 : BinaryPrimitives.ReadInt32LittleEndian(entry.Slice(12, 4));
            if (!groupResource && (bytesInResource <= 0 || offset < 0 || offset + bytesInResource > bytes.Length))
                throw new InvalidDataException($"{width}px 图层范围越界。");
            result.Add(new IconEntry(width, bitCount, bytesInResource, offset));
        }
        return result;
    }

    private static void AssertSizes(IEnumerable<int> actualSizes, string source)
    {
        var actual = actualSizes.ToArray();
        if (!actual.SequenceEqual(RequiredSizes))
            throw new InvalidDataException($"{source} 图层应为 [{string.Join(", ", RequiredSizes)}]，实际为 [{string.Join(", ", actual)}]。");
    }

    private readonly record struct IconEntry(int Size, int BitCount, int BytesInResource, int Offset);

    private delegate bool EnumResNameProc(IntPtr module, IntPtr type, IntPtr name, IntPtr parameter);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr LoadLibraryEx(string fileName, IntPtr file, uint flags);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool FreeLibrary(IntPtr module);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool EnumResourceNames(IntPtr module, IntPtr type, EnumResNameProc callback, IntPtr parameter);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr FindResource(IntPtr module, IntPtr name, IntPtr type);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint SizeofResource(IntPtr module, IntPtr resource);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr LoadResource(IntPtr module, IntPtr resource);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LockResource(IntPtr resourceData);
}
