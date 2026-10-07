import os
import csv
import glob

print(f"{'Strategy (CG + CS)':<35} | {'Budget/k 1 (Fast)':<17} | {'Budget/k 2 (Regression)':<25}")
print("-" * 80)

for file in sorted(glob.glob("results/tpccWorkloadRead_*_avg.csv")):
    # Extract cg and cs from filename
    # Format: tpccWorkloadRead_cg_ben_knap_cs_drop_avg.csv
    basename = os.path.basename(file)
    parts = basename.replace("tpccWorkloadRead_", "").replace("_avg.csv", "")
    
    # Read the CSV
    with open(file, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        
    if not rows:
        continue
        
    key_col = 'storage_mb' if 'storage_mb' in rows[0] else 'k'
    
    # Find the minimum time (usually at low budget)
    min_row = min(rows, key=lambda r: float(r['avg_total_time_seconds']) if float(r['avg_total_time_seconds']) > 0 else float('inf'))
    
    # Find the maximum time (usually at higher budget)
    max_row = max(rows, key=lambda r: float(r['avg_total_time_seconds']))
    
    if float(max_row['avg_total_time_seconds']) < 0:
        continue # Ignored timeouts
        
    print(f"{parts:<35} | {min_row[key_col]:>5} -> {float(min_row['avg_total_time_seconds']):>6.1f}s | {max_row[key_col]:>5} -> {float(max_row['avg_total_time_seconds']):>6.1f}s")
