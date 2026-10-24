import argparse,json
from pathlib import Path
from collections import Counter
p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
c=Counter()
for f in a.input.glob('P2-*.json'):
 for row in json.loads(f.read_text())['rows']:c[row['kind'],row['symbol_offset']]+=row['cycles']
a.output.write_text(json.dumps([dict(kind=k[0],symbol_offset=k[1],pooled_cycles=v) for k,v in c.most_common()],indent=2)+'\n')
print('Prepared',len(c),'unique weighted source offsets')
