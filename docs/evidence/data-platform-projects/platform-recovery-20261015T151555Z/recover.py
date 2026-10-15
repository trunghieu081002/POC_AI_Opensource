import os,sys,re,json,hashlib
from pathlib import Path
sys.path[:0]=['/opt/dpagent/src','/opt/dpagent/tests/m25_acceptance']
import run_matrix as m
from dpagent.pipelines import bronze,loader
creds=m._seaweed_credentials()
os.environ.update({'HG_POC_BRONZE_ENDPOINT':creds['S3_ENDPOINT'],'HG_POC_BRONZE_ACCESS_KEY':creds['S3_ACCESS_KEY'],'HG_POC_BRONZE_SECRET_KEY':creds['S3_SECRET_KEY'],'HG_POC_BRONZE_BUCKET':'hg-bronze'})
for name in ('HG_POC_WH_USER','HG_POC_WH_PASSWORD','HG_POC_SOURCE_HOST','HG_POC_SOURCE_NAME','HG_POC_SOURCE_USER','HG_POC_SOURCE_PASSWORD'):os.environ.setdefault(name,'unused')
p=loader.load('hg_resources_useage_number',Path('/root/data-platform-evidence/pipeline-sources'))
keys=[k for k in m._s3py('list','hg-bronze','bronze/').stdout.splitlines() if k.strip()]
prefixes=sorted(set('/'.join(k.split('/')[:2]) for k in keys))
for prefix in prefixes:
 if prefix!='bronze/'+p.name:raise RuntimeError('Unexpected legacy fixture namespace; refusing deletion')
 bronze.purge_namespace(p,prefix)
 if bronze.count_namespace(p,prefix)!=0:raise RuntimeError('Legacy fixture namespace remains')
print(json.dumps({'legacy_namespaces_purged':prefixes,'remaining_owned_objects':len([k for k in m._s3py('list','hg-bronze','bronze/').stdout.splitlines() if k.strip()])}))
# This file belongs only to the verified disposable test container. The old
# test reused example HG_POC names; remove precisely that failed attempt's keys,
# after its DAGs/publication have actually been removed. Keep no secret values.
f=Path('/opt/airflow/home/pipelines.env');raw=f.read_bytes();lines=raw.decode().splitlines();removed=[];kept=[]
for line in lines:
 key=line.split('=',1)[0].removeprefix('export ').strip()
 if key.startswith('HG_POC_'):removed.append(key)
 else:kept.append(line)
f.write_text('\n'.join(kept)+'\n');f.chmod(0o640)
print(json.dumps({'owned_legacy_env_keys_removed':sorted(removed),'before_sha256':hashlib.sha256(raw).hexdigest(),'after_sha256':hashlib.sha256(f.read_bytes()).hexdigest()}))
