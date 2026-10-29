# 生成「一键启动」的桌面快捷方式。
#
# 为什么不能直接固定 Galgame_monster.bat：
#   1) Windows 的任务栏固定只对「可执行程序」开放，.bat / .cmd 没有「固定到任务栏」这一项；
#   2) 那个 .bat 是 start ... 之后立刻退出的，没有常驻窗口；
#   3) 真正开窗的是 desktop/ 下的 electron.exe（壳不可用时才回退浏览器 --app），
#      任务栏按「AppUserModelID」归组，所以按钮会属于 electron.exe 而不是这个 .bat。
# 把快捷方式的目标换成 pythonw.exe（exe），固定这一项就没问题了。
#
# 为什么还要写 AppUserModelID：
#   Windows 10/11 的任务栏按钮图标按 AUMID 去找「同 ID 的快捷方式」的图标，
#   光在 Electron 里设 BrowserWindow.icon 是不生效的（实测）。
#   这里写入的 ID 必须和 desktop/main.js、app/win_shortcut.py 里的完全一致：
#       GalgameMonster.Desktop
#
# 用法（在项目根目录）：
#   powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
# 生成后：右键桌面上的 Galgame_monster →「固定到任务栏」。
#
# 注意：快捷方式里存的是绝对路径，项目文件夹移动后请重新生成。
#      （原来的 .bat 靠 %~dp0 自定位，不受移动影响，两者可以并存。）

param(
    # 输出目录；默认桌面。传其它目录可生成到别处（例如开始菜单）。
    [string]$OutDir = '',
    # 与 desktop/main.js、app/win_shortcut.py 保持一致
    [string]$Aumid = 'GalgameMonster.Desktop'
)

$ErrorActionPreference = 'Stop'

# 脚本位于 <项目根>\scripts，所以往上退一级就是项目根
$Root = Split-Path -Parent $PSScriptRoot

$py = Join-Path $Root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $py)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $py = $cmd.Source
    } else {
        throw '找不到 pythonw.exe（.venv\Scripts 与 PATH 里都没有）。请先按 readme 建好虚拟环境。'
    }
}

$main = Join-Path $Root 'main.py'
if (-not (Test-Path $main)) {
    throw "找不到 main.py：$main"
}

if (-not $OutDir) {
    $OutDir = [Environment]::GetFolderPath('Desktop')
}
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
}

$icon = Join-Path $Root 'app.ico'
$lnk = Join-Path $OutDir 'Galgame_monster.lnk'

# WScript.Shell 设不了 System.AppUserModel.ID，只能走 COM（IShellLinkW + IPropertyStore）
$src = @'
using System;
using System.Runtime.InteropServices;
using System.Text;

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
public class CShellLink { }

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IShellLinkW
{
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cch, IntPtr pfd, uint fFlags);
    void GetIDList(out IntPtr ppidl);
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
    void GetHotkey(out short pwHotkey);
    void SetHotkey(short wHotkey);
    void GetShowCmd(out int piShowCmd);
    void SetShowCmd(int iShowCmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cch, out int piIcon);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
    void Resolve(IntPtr hwnd, uint fFlags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
}

[StructLayout(LayoutKind.Sequential)]
public struct PROPERTYKEY { public Guid fmtid; public int pid; }

[StructLayout(LayoutKind.Explicit)]
public struct PROPVARIANT
{
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr pointerValue;
}

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPropertyStore
{
    void GetCount(out uint cProps);
    void GetAt(uint iProp, out PROPERTYKEY pkey);
    void GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    void SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    void Commit();
}

public static class Lnk
{
    public static void Create(string lnk, string target, string args, string iconPath,
                              string workDir, string aumid)
    {
        var link = (IShellLinkW)new CShellLink();
        link.SetPath(target);
        if (args.Length > 0) link.SetArguments(args);
        if (workDir.Length > 0) link.SetWorkingDirectory(workDir);
        link.SetDescription("Galgame_monster");
        link.SetIconLocation(iconPath, 0);
        System.IO.Directory.CreateDirectory(System.IO.Path.GetDirectoryName(lnk));

        var store = (IPropertyStore)link;
        var key = new PROPERTYKEY();
        key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");   // System.AppUserModel
        key.pid = 5;                                                      // .ID
        var pv = new PROPVARIANT();
        pv.vt = 31;                                                       // VT_LPWSTR
        pv.pointerValue = Marshal.StringToCoTaskMemUni(aumid);
        try
        {
            store.SetValue(ref key, ref pv);
            store.Commit();
            var persist = (System.Runtime.InteropServices.ComTypes.IPersistFile)link;
            persist.Save(lnk, true);
        }
        finally
        {
            Marshal.FreeCoTaskMem(pv.pointerValue);
        }
    }
}
'@

Add-Type -TypeDefinition $src -Language CSharp
[Lnk]::Create($lnk, $py, ('"' + $main + '"'), $icon, $Root, $Aumid)

Write-Host "已生成快捷方式：$lnk"
Write-Host "  目标    ：$py"
Write-Host "  参数    ：`"$main`""
Write-Host "  起始位置：$Root"
Write-Host "  图标    ：$icon"
Write-Host "  AUMID   ：$Aumid"
Write-Host ''
Write-Host '下一步：右键这个快捷方式 →「固定到任务栏」。'
