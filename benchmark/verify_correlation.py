"""Real W3C probe and backend evidence; fail if either service/SQL/span linkage is missing."""
import argparse
import json
from pathlib import Path
import secrets
import time
import urllib.parse
import urllib.request

def get(url):
    with urllib.request.urlopen(url, timeout=15) as r: return json.load(r)

def verify(output):
    output.mkdir(parents=True, exist_ok=True)
    trace_id, parent_id = secrets.token_hex(16), secrets.token_hex(8)
    start = time.time_ns()
    req = urllib.request.Request('http://127.0.0.1:18000/orders', method='POST',
        data=json.dumps({'customer_id':'correlation-probe','amount':125000,'currency':'COP'}).encode(),
        headers={'Content-Type':'application/json','traceparent':f'00-{trace_id}-{parent_id}-01'})
    with urllib.request.urlopen(req, timeout=15) as res: order=json.load(res)
    assert order['status'] == 'PAID', order
    query = '{service_name=~"orders-service|payments-service"} | trace_id="'+trace_id+'"'
    last = None
    for _ in range(30):
        try:
            logs = get('http://127.0.0.1:13100/loki/api/v1/query_range?'+urllib.parse.urlencode(
                {'query':query,'start':str(start-10_000_000_000),'end':str(time.time_ns()),'limit':'100'}))
            traces = get('http://127.0.0.1:16687/api/traces/'+trace_id)
            trace = traces['data'][0]
            spans = {s['spanID']:s for s in trace['spans']}
            trace_services = {p['serviceName'] for p in trace['processes'].values()}
            records=[]
            for stream in logs['data']['result']:
                for value in stream['values']:
                    record=json.loads(value[1])
                    if record.get('event') in {'order.persisted','order.completed','payment.authorized'}:
                        assert record['trace_id'] == trace_id
                        assert record['span_id'] in spans
                        assert record['order_id'] == order['order_id']
                        records.append(record)
            assert {r['service'] for r in records} == {'orders-service','payments-service'}
            assert {'orders-service','payments-service'} <= trace_services
            assert any(s['operationName'] == 'order.create' for s in spans.values())
            assert any(s['operationName'] == 'payment.authorize' for s in spans.values())
            assert any(any(t['key'] in ('db.system','db.system.name','db.statement','db.query.text') for t in s['tags']) for s in spans.values())
            # At least one CHILD_OF edge crosses from orders to payments.
            def svc(s): return trace['processes'][s['processID']]['serviceName']
            assert any(svc(s)=='payments-service' and any(
                r['refType']=='CHILD_OF' and r['spanID'] in spans and svc(spans[r['spanID']])=='orders-service'
                for r in s['references']) for s in spans.values())
            assert any(r['spanID']==parent_id for s in spans.values() for r in s['references'])
            (output/'loki.json').write_text(json.dumps(logs,indent=2),encoding='utf-8')
            (output/'jaeger.json').write_text(json.dumps(traces,indent=2),encoding='utf-8')
            evidence={'trace_id':trace_id,'parent_id':parent_id,'order_id':order['order_id'],
                      'logql':query,'spans':len(spans),'matched_logs':len(records),'status':'PASS',
                      'jaeger_url':'http://localhost:16687/trace/'+trace_id}
            (output/'verification.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
            pane={'logs':{'datasource':'loki','queries':[{'refId':'A','expr':query,'queryType':'range','datasource':{'type':'loki','uid':'loki'}}],
                          'range':{'from':str(start//1000000-60000),'to':str(time.time_ns()//1000000+60000)}}}
            url='http://localhost:13000/explore?schemaVersion=1&panes='+urllib.parse.quote(json.dumps(pane))+'&orgId=1'
            (output/'abrir-grafana.url').write_text('[InternetShortcut]\nURL='+url+'\n')
            print(json.dumps(evidence,indent=2)); return
        except Exception as exc:
            last=exc; time.sleep(2)
    raise RuntimeError(f'Correlacion no verificada para {trace_id}: {last!r}')

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',type=Path,default=Path('benchmark/results/correlation'))
    verify(p.parse_args().output)
