' Laguna Translator — launcher silencioso (sem janela de console)
' Roda laguna_server.py via pythonw (sem console) no diretorio do script
Dim sh, fso, here
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

' Usa pythonw.exe (sem janela). Fallback para python.exe se nao existir.
Dim pyw
pyw = "C:\Python313\pythonw.exe"
If Not fso.FileExists(pyw) Then
    pyw = "C:\Python313\python.exe"
End If

sh.CurrentDirectory = here
sh.Run """" & pyw & """ """ & here & "\laguna_server.py""", 0, False
