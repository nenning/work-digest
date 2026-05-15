@echo off
REM Registers .ps1 files to run with PowerShell (per-user, no admin required).
REM Sets execution policy to RemoteSigned for current user.

powershell -NonInteractive -Command "$null=New-Item -Path 'HKCU:\SOFTWARE\Classes\.ps1' -Force; Set-ItemProperty 'HKCU:\SOFTWARE\Classes\.ps1' '(Default)' 'Microsoft.PowerShellScript.1' -Force; $cmd='HKCU:\SOFTWARE\Classes\Microsoft.PowerShellScript.1\Shell\Open\Command'; $null=New-Item -Path $cmd -Force; Set-ItemProperty $cmd '(Default)' 'powershell.exe -NonInteractive -File \"%1\" %*' -Force; Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force; Write-Host 'Done: .ps1 files now run with PowerShell for this user.'"
