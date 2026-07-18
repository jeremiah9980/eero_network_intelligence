import csv, zipfile, tempfile, os
from .db import upsert_device

def import_dsar(zip_path, con):
    count=0
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zip_path) as z: z.extractall(td)
        for fn in os.listdir(td):
            if fn.endswith('-clients.csv') or fn=='devices.csv':
                with open(os.path.join(td,fn), newline='') as f:
                    for row in csv.DictReader(f):
                        if fn=='devices.csv' and row.get('mac','').isdigit():
                            n=int(row['mac']); row['MAC Address']=':'.join(f'{(n>>s)&255:02x}' for s in range(40,-1,-8))
                        d={
                          'mac': row.get('MAC Address') or row.get('mac'),
                          'name': row.get('Nickname') or row.get('nickname') or row.get('Hostname'),
                          'hostname': row.get('Hostname'),
                          'vendor': row.get('Org Name'),
                          'profile': row.get('profileName'),
                          'network_id': row.get('Network ID'),
                          'last_seen': row.get('Last Seen At'),
                          'created': row.get('created'),
                          **row}
                        upsert_device(con,d); count+=1
    return count
