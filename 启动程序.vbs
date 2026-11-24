' 无窗口启动器：双击本文件即可静默启动程序，不显示黑色命令行窗口。
' 原理：用 WScript.Shell.Run 的窗口模式 0（完全隐藏）启动服务，
'       输出重定向到 logs/，便于排错。
Option Explicit
Dim fso, shell, proj, logOut, logErr, command
Set fso  = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

proj   = fso.GetParentFolderName(WScript.ScriptFullName)
logOut = proj & "\logs\server.out"
logErr = proj & "\logs\server.err"

' 确保日志目录存在
If Not fso.FolderExists(proj & "\logs") Then fso.CreateFolder proj & "\logs"

shell.CurrentDirectory = proj

' 用 cmd /c 启动并把输出写入日志；Run 第二参数 0 = 隐藏窗口，False = 不等待
command = "cmd /c ""python.exe """ & proj & "\server.py"" > """ & logOut & """ 2> """ & logErr & """"""
shell.Run command, 0, False
