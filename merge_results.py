import os
import re
import csv
import glob

def get_col_name(cs, i_str):
    if 'greedy' in cs:
        k_val = i_str.split('_')[0]
        return f"avg_time_seconds(k={k_val})"
    else:
        b_val = i_str.replace('_qt', '')
        return f"avg_time_seconds(storage_budget={b_val})"

def extract_num(i_str):
    try:
        return float(i_str.split('_')[0])
    except ValueError:
        return 0

def sort_query_key(q):
    match = re.match(r'([a-zA-Z]+)(\d+)(.*)', q)
    if match:
        return (match.group(1), int(match.group(2)), match.group(3))
    return (q, 0, "")

def process():
    os.makedirs('results2', exist_ok=True)
    files = glob.glob('results/*_qt.csv')
    
    # Regex to extract parts
    pattern = re.compile(r'^(.*?)_(cg_.*?)_(cs_.*?)_([\d\.]+(?:_\d+)?_qt)\.csv$')
    
    groups = {}
    
    for f in files:
        basename = os.path.basename(f)
        m = pattern.match(basename)
        if not m:
            continue
            
        workload = m.group(1)
        cg = m.group(2)
        cs = m.group(3)
        i = m.group(4)
        
        key = (workload, cg, cs)
        if key not in groups:
            groups[key] = []
        groups[key].append((i, f))
        
    print(f"Found {len(groups)} groups. Coalescing...")
    
    for (workload, cg, cs), items in groups.items():
        # Sort items by numeric value
        items.sort(key=lambda x: extract_num(x[0]))
        
        col_names = []
        data = {}
        
        for i_str, fpath in items:
            col_name = get_col_name(cs, i_str)
            col_names.append(col_name)
            
            with open(fpath, 'r', newline='') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) >= 2:
                        q = row[0]
                        val = row[1]
                        if q not in data:
                            data[q] = {}
                        data[q][col_name] = val
                        
        out_fpath = os.path.join('results2', f"{workload}_{cg}_{cs}.csv")
        with open(out_fpath, 'w', newline='') as f:
            writer = csv.writer(f)
            header = ['query'] + col_names
            writer.writerow(header)
            
            sorted_queries = sorted(data.keys(), key=sort_query_key)
            for q in sorted_queries:
                row = [q]
                for col in col_names:
                    row.append(data[q].get(col, "-1"))
                writer.writerow(row)
                
    print("Done. Coalesced files are in results2/")

if __name__ == '__main__':
    process()
