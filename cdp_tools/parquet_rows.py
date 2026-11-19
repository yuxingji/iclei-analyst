"""Emit parquet rows one at a time to avoid a giant JSON string in Node."""
import json,sys
import pyarrow.parquet as pq
sys.stdout.reconfigure(encoding='utf-8')

for batch in pq.ParquetFile(sys.argv[1]).iter_batches(batch_size=250):
    for row in batch.to_pylist():
        print(json.dumps(row,ensure_ascii=False,default=str),flush=False)
