Option Explicit
Dim shell, probe, projectPath, runScript
projectPath = "C:\PROJECT-DBMS\FILEBOX"
runScript = projectPath & "\run.ps1"
Set shell = CreateObject("WScript.Shell")

' Check whether FileBox is already healthy; if so, just open it.
On Error Resume Next
Set probe = CreateObject("WinHttp.WinHttpRequest.5.1")
If Err.Number = 0 Then
  probe.SetTimeouts 1000, 1000, 1000, 1000
  probe.Open "GET", "http://127.0.0.1:80/healthz", False
  probe.Send
  If Err.Number = 0 Then
    If probe.Status = 200 Then
      shell.Run "http://127.0.0.1:80", 1, False
      WScript.Quit 0
    End If
  End If
End If
Err.Clear
On Error GoTo 0

' Start FileBox silently; the server opens the browser when ready.
shell.CurrentDirectory = projectPath
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & runScript & """", 0, False
