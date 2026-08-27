using System.Drawing.Drawing2D;
using System.Drawing.Imaging;

if (args.Length != 2) throw new ArgumentException("用法：IconGenerator <源 PNG> <目标 ICO>");
var sizes = new[] { 16, 24, 32, 48, 64, 128, 256 };
using var source = Image.FromFile(args[0]);
var payloads = new List<byte[]>();
foreach (var size in sizes)
{
    using var bitmap = new Bitmap(size, size, PixelFormat.Format32bppArgb);
    using (var graphics = Graphics.FromImage(bitmap))
    {
        graphics.CompositingQuality = CompositingQuality.HighQuality;
        graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
        graphics.SmoothingMode = SmoothingMode.HighQuality;
        graphics.DrawImage(source, 0, 0, size, size);
    }
    using var stream = new MemoryStream();
    bitmap.Save(stream, ImageFormat.Png);
    payloads.Add(stream.ToArray());
}
Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(args[1]))!);
using var output = new BinaryWriter(File.Create(args[1]));
output.Write((ushort)0); output.Write((ushort)1); output.Write((ushort)sizes.Length);
var offset = 6 + sizes.Length * 16;
for (var index = 0; index < sizes.Length; index++)
{
    var size = sizes[index];
    output.Write((byte)(size == 256 ? 0 : size));
    output.Write((byte)(size == 256 ? 0 : size));
    output.Write((byte)0); output.Write((byte)0);
    output.Write((ushort)1); output.Write((ushort)32);
    output.Write(payloads[index].Length); output.Write(offset);
    offset += payloads[index].Length;
}
foreach (var payload in payloads) output.Write(payload);
