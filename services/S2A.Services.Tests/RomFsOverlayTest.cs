// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using LibHac;
using LibHac.Fs;
using System;
using System.IO;
using System.Text;
using Path = System.IO.Path;

namespace S2A.Services.Tests
{
    // 界面布局覆盖层（Windows 编排 uilayout 写 <产物根>/ui_overlay/romfs/，索引按覆盖层大小建，pack/orch/stages.py _romfsidx）：
    //   宿主读方必须覆盖层优先。11-07 回归批动森 / 王国之泪：Windows 出包机出的产物根在 Mac 宿主局读 romfs/ 原件，索引 871 实际 839 → 2002-4703 中止。
    // 期望值不跑被测实现：两份文件是本测写下的固定字节（原件 1000×0x11、覆盖层 1200×0x22），索引清单照 stages.py 的「大小\t路径」写。
    static class RomFsOverlayTest
    {
        [SoloTest("--romfs-overlay")]
        public static int Run()
        {
            string root = Path.Combine(Path.GetTempPath(), $"s2a_romfs_overlay_{Environment.ProcessId}");
            string rom = Path.Combine(root, "romfs"), over = Path.Combine(root, "ui_overlay", "romfs");
            bool strict = Red.Strict;
            int failures = 0;
            void Check(string name, string expected, string actual)
            {
                bool ok = expected == actual;
                if (!ok) failures++;
                Console.WriteLine($"{name}：期望 {expected} → 实 {actual} {(ok ? "✓" : "✗")}");
            }
            static string Read(IStorage image, string path, int len)
            {
                var fs = new LibHac.Tools.FsSystem.RomFs.RomFsFileSystem(image);
                if (!fs.FileTable.TryOpenFile(path, out LibHac.Tools.FsSystem.RomFs.RomFileInfo info)) return "镜像里没有 " + path;
                byte[] b = new byte[len];
                Result r = image.Read(fs.Header.DataOffset + info.Offset, b);
                if (r.IsFailure()) return r.ToString();
                return info.Length == len && Array.TrueForAll(b, x => x == b[0]) ? $"Success/{len}×0x{b[0]:X2}" : $"Success/长度 {info.Length} 内容不齐";
            }
            try
            {
                Red.Strict = false;   // 负对照的红行（长度不符）不终止本测
                Directory.CreateDirectory(Path.Combine(rom, "Layout"));
                Directory.CreateDirectory(Path.Combine(over, "Layout"));
                byte[] baseBytes = new byte[1000]; Array.Fill(baseBytes, (byte)0x11);
                byte[] overBytes = new byte[1200]; Array.Fill(overBytes, (byte)0x22);
                byte[] other = new byte[64]; Array.Fill(other, (byte)0x33);
                File.WriteAllBytes(Path.Combine(rom, "Layout", "a.zs"), baseBytes);
                File.WriteAllBytes(Path.Combine(over, "Layout", "a.zs"), overBytes);
                File.WriteAllBytes(Path.Combine(rom, "b.bin"), other);
                string list = Path.Combine(root, "list.tsv"), index = Path.Combine(root, "romfs.s2aidx");
                File.WriteAllText(list, "1200\tLayout/a.zs\n64\tb.bin\n", new UTF8Encoding(false));
                RomFsIndex.Write(rom, index, list);

                using (IStorage s = RomFsIndex.Open(rom, index, over))
                {
                    Check("索引 + 覆盖层：覆盖文件", "Success/1200×0x22", Read(s, "/Layout/a.zs", 1200));
                    Check("索引 + 覆盖层：未覆盖文件", "Success/64×0x33", Read(s, "/b.bin", 64));
                }
                using (IStorage s = RomFsIndex.Open(rom, index))
                {
                    Check("负对照：索引不叠覆盖层（11-07 现象）", $"{ResultFs.HostFileDataCorrupted.Value}", Read(s, "/Layout/a.zs", 1200).Split('/')[0]);
                }
                using (IStorage s = S2A.Services.Application.RomFsFromDirectory(rom, over))
                {
                    Check("目录现建 + 覆盖层：覆盖文件", "Success/1200×0x22", Read(s, "/Layout/a.zs", 1200));
                }
                // #玩家诊断：romfs 里的 macOS 垃圾进了索引、却不进资源包，游戏枚举到就读不到（万缕千丝 Movie/.DS_Store，服务异常后闪退）；游戏自己的点开头文件要留
                Directory.CreateDirectory(Path.Combine(rom, "Movie"));
                File.WriteAllBytes(Path.Combine(rom, ".DS_Store"), other);
                File.WriteAllBytes(Path.Combine(rom, "Movie", ".DS_Store"), other);
                File.WriteAllBytes(Path.Combine(rom, "Movie", "._a.mp4"), other);
                File.WriteAllBytes(Path.Combine(rom, ".build.config"), other);
                File.WriteAllBytes(Path.Combine(over, ".DS_Store"), other);
                string Listed(IStorage s, string path) => new LibHac.Tools.FsSystem.RomFs.RomFsFileSystem(s).FileTable.TryOpenFile(path, out LibHac.Tools.FsSystem.RomFs.RomFileInfo _) ? "在" : "不在";
                string junkIndex = Path.Combine(root, "junk.s2aidx");
                RomFsIndex.Write(rom, junkIndex);
                foreach ((string name, IStorage image) in new[] { ("索引", RomFsIndex.Open(rom, junkIndex)), ("目录现建", S2A.Services.Application.RomFsFromDirectory(rom, over)) })
                {
                    using (image)
                    {
                        Check($"{name}：根目录 .DS_Store", "不在", Listed(image, "/.DS_Store"));
                        Check($"{name}：子目录 .DS_Store", "不在", Listed(image, "/Movie/.DS_Store"));
                        Check($"{name}：._ 文件", "不在", Listed(image, "/Movie/._a.mp4"));
                        Check($"{name}：游戏自己的点开头文件", "在", Listed(image, "/.build.config"));
                    }
                }
                Check("覆盖层盘点不收 .DS_Store", "1", RomFsIndex.OverlayFiles(over).Count.ToString());
            }
            finally { Red.Strict = strict; Directory.Delete(root, true); }
            return failures == 0 ? 0 : 1;
        }
    }
}
