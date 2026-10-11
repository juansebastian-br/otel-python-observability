param([int]$Usuarios = 50, [int]$Repeticiones = 1)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python benchmark/run.py --vus $Usuarios --repetitions $Repeticiones
if ($LASTEXITCODE -ne 0) { throw 'La ejecucion fallo. Comparte el error antes de usar los resultados.' }
Write-Host 'Resultados en benchmark/results. Puedes volver al chat para completar el informe.'
