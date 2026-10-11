"""Validate provisioning by API. Does not claim a visual test of Explore."""
import argparse
import json
from pathlib import Path
import re
import urllib.request

def get(path):
    with urllib.request.urlopen('http://127.0.0.1:13000'+path,timeout=15) as r:
        return json.load(r)

def verify(output):
    output.mkdir(parents=True,exist_ok=True)
    sources={uid:get('/api/datasources/uid/'+uid) for uid in ('loki','jaeger')}
    health=get('/api/datasources/uid/loki/health')
    assert health['status']=='OK',health
    field=sources['loki']['jsonData']['derivedFields'][0]
    assert field['datasourceUid']=='jaeger'
    assert field['url']=='${__value.raw}',field
    trace='1234567890abcdef1234567890abcdef'
    assert re.search(field['matcherRegex'],json.dumps({'trace_id':trace})).group(1)==trace
    inverse=sources['jaeger']['jsonData']['tracesToLogsV2']
    assert inverse['datasourceUid']=='loki'
    assert '${__span.traceId}' in inverse['query']
    result={'status':'PASS','scope':'API provisioning and Loki health; no visual UI assertion',
            'datasources':sources,'loki_health':health}
    (output/'grafana-api.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('PASS: Loki health, derived field regex, Jaeger UID and reverse query via Grafana API')

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',type=Path,required=True)
    verify(p.parse_args().output)
