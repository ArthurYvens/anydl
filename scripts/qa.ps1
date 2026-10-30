$ErrorActionPreference = "Stop"

python -m compileall .
python -m pytest -q

$ruff = Get-Command ruff -ErrorAction SilentlyContinue
if ($ruff) {
    ruff check .
} else {
    Write-Host "Ruff not installed; skipping lint."
}
