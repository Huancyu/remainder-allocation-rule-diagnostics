$ErrorActionPreference = "Stop"

python src/generate_sample_data.py
python src/diagnose.py --input data/sample_preprocessed.csv --output-dir outputs
python src/build_reports.py --input outputs/diagnostics.json --output-dir outputs

Write-Host "Demo completed:"
Write-Host "  outputs/rule_diagnostics.xlsx"
Write-Host "  outputs/loss_distribution.html"

