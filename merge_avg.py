import os
import re
import csv
import glob

def extract_num(val_str):
    try:
        return float(val_str)
    except:
        return 0

def process():
    files = glob.glob('results/*_avg.csv')
    
    # regex to match {workload}_{cg}_{cs}_avg.csv
    # Note: sometimes cg or cs can have multiple parts. 
    # We can use the same regex approach: ^(.*?)_(cg_.*?)_(cs_.*?)_avg\.csv$
    pattern = re.compile(r'^(.*?)_(cg_.*?)_(cs_.*?)_avg\.csv$')
    
    workloads = {}
    
    for f in files:
        basename = os.path.basename(f)
        m = pattern.match(basename)
        if not m:
            continue
            
        workload = m.group(1)
        cg = m.group(2)
        cs = m.group(3)
        
        if workload not in workloads:
            workloads[workload] = []
        workloads[workload].append((cg, cs, f))
        
    for workload, items in workloads.items():
        data = {} # config_val -> { "cg_cs_col": val }
        strategies = []
        
        for cg, cs, fpath in items:
            strategy = f"{cg}_{cs}"
            strategies.append(strategy)
            
            with open(fpath, 'r', newline='') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    continue
                # header is something like [k/storage_mb, avg_total_time_seconds, index_size_bytes, index_size_mb]
                
                for row in reader:
                    if len(row) >= 2:
                        try:
                            c_val = float(row[0])
                        except:
                            continue
                        
                        if c_val not in data:
                            data[c_val] = {}
                            
                        # Extract metrics
                        # Let's save avg_total_time_seconds and index_size_mb
                        # based on header positions, or just assume standard
                        avg_time = row[1] if len(row) > 1 else "-1"
                        idx_mb = row[3] if len(row) > 3 else "-1"
                        
                        data[c_val][f"{strategy}_avg_total_time_seconds"] = avg_time
                        data[c_val][f"{strategy}_index_size_mb"] = idx_mb
                        
        out_fpath = os.path.join('results2', f"{workload}_avg_combined.csv")
        with open(out_fpath, 'w', newline='') as f:
            writer = csv.writer(f)
            
            # Build header
            # strategies sorted to ensure deterministic order
            strategies.sort()
            out_header = ['config_val']
            for st in strategies:
                out_header.append(f"{st}_avg_total_time_seconds")
                out_header.append(f"{st}_index_size_mb")
                
            writer.writerow(out_header)
            
            # Sort config_val
            for c_val in sorted(data.keys()):
                row_out = [c_val]
                for st in strategies:
                    row_out.append(data[c_val].get(f"{st}_avg_total_time_seconds", "-1"))
                    row_out.append(data[c_val].get(f"{st}_index_size_mb", "-1"))
                writer.writerow(row_out)

if __name__ == '__main__':
    process()
