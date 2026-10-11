"""Convert real k6 summaries + external Docker samples into an auditable comparison."""
import csv
import json
from pathlib import Path
import statistics
import sys

def analyze(root):
    meta=json.loads((root/'metadata.json').read_text())
    rows=[]
    for arm in sorted(root.glob('[0-9][0-9]-*')):
        summary=json.loads((arm/'summary.json').read_text())
        state=json.loads((arm/'run.json').read_text())
        metrics=summary['metrics']
        def values(name): return metrics[name].get('values',metrics[name])
        groups={}
        with (arm/'resources.csv').open(encoding='utf-8') as f:
            for row in csv.DictReader(f):
                groups.setdefault(row['timestamp_utc'],{})[row['service']]=row
        services=('orders-service','payments-service','otel-collector','postgres','loki','jaeger','prometheus','grafana')
        complete=[g for g in groups.values() if all(s in g for s in services)]
        if len(complete)<3: raise ValueError(f'Faltan muestras de recursos en {arm}')
        cpu=[sum(float(g[s]['cpu_percent']) for s in ('orders-service','payments-service')) for g in complete]
        mem=[sum(float(g[s]['memory_mib']) for s in ('orders-service','payments-service')) for g in complete]
        row={'run':arm.name,'mode':state['mode'],'p99_ms':values('http_req_duration')['p(99)'],
             'p95_ms':values('http_req_duration')['p(95)'],'rps':values('http_reqs')['rate'],
             'requests':values('http_reqs')['count'],'error_rate':values('http_req_failed')['rate'],
             'checks_rate':values('checks')['rate'],'cpu_percent_mean':statistics.mean(cpu),
             'memory_mib_mean':statistics.mean(mem),'memory_mib_peak':max(mem),'samples':len(complete),
             'collector_cpu_percent_mean':statistics.mean(float(g['otel-collector']['cpu_percent']) for g in complete),
             'collector_memory_mib_mean':statistics.mean(float(g['otel-collector']['memory_mib']) for g in complete),
             'stack_cpu_percent_mean':statistics.mean(sum(float(g[s]['cpu_percent']) for s in services) for g in complete),
             'stack_memory_mib_mean':statistics.mean(sum(float(g[s]['memory_mib']) for s in services) for g in complete),
             'valid':state['k6_exit_code']==0 and not state['sampling_errors'] and values('http_req_failed')['rate']<0.01}
        rows.append(row)
    if not rows: raise ValueError('No hay ejecuciones completas')
    with (root/'comparacion.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=rows[0]); w.writeheader(); w.writerows(rows)
    agg={mode:{key:statistics.median(r[key] for r in rows if r['mode']==mode)
               for key in ('p99_ms','cpu_percent_mean','memory_mib_mean','memory_mib_peak','rps','error_rate','stack_cpu_percent_mean','stack_memory_mib_mean')}
         for mode in ('baseline','otel')}
    b,o=agg['baseline'],agg['otel']
    delta={'p99_additional_ms':o['p99_ms']-b['p99_ms'],
           'p99_overhead_percent':(o['p99_ms']/b['p99_ms']-1)*100 if b['p99_ms'] else None,
           'cpu_overhead_percent':(o['cpu_percent_mean']/b['cpu_percent_mean']-1)*100 if b['cpu_percent_mean'] else None,
           'cpu_percentage_points':o['cpu_percent_mean']-b['cpu_percent_mean'],
           'memory_additional_mib':o['memory_mib_mean']-b['memory_mib_mean'],
           'stack_memory_additional_mib':o['stack_memory_mib_mean']-b['stack_memory_mib_mean']}
    result={'aggregate':agg,'delta':delta,'runs':rows,'valid':all(r['valid'] for r in rows),
            'scope':'local Docker Desktop; application metrics: orders+payments; stack metrics: all eight containers; median of per-run measurements'}
    (root/'comparacion.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    def fmt(v): return 'No definido' if v is None else f'{v:.3f}'
    lines=['# Resultados locales de overhead','',f"Carga: {meta['vus']} VUs, {meta['duration_seconds']} s por escenario.",
           '', '| Medida | Sin OTel | Con OTel | Diferencia |','|---|---:|---:|---:|']
    for label,key,diff in [('p99 (ms)','p99_ms',delta['p99_additional_ms']),
                           ('CPU media (% de un nucleo)','cpu_percent_mean',delta['cpu_percentage_points']),
                           ('Memoria media (MiB)','memory_mib_mean',delta['memory_additional_mib']),
                           ('Memoria total laboratorio (MiB)','stack_memory_mib_mean',delta['stack_memory_additional_mib']),
                           ('Throughput (req/s)','rps',o['rps']-b['rps']),
                           ('Fraccion de errores','error_rate',o['error_rate']-b['error_rate'])]:
        lines.append(f'| {label} | {fmt(b[key])} | {fmt(o[key])} | {fmt(diff)} |')
    lines+=['',f"CPU overhead relativo: {fmt(delta['cpu_overhead_percent'])}%.",
            f"p99 overhead relativo: {fmt(delta['p99_overhead_percent'])}%.",
            f"Validez de carga/muestreo: {'PASS' if result['valid'] else 'FAIL - revisar logs antes de concluir'}.",
            '', 'CPU y memoria de aplicaciones suman orders + payments. Collector se informa por separado en el CSV. La memoria total suma los ocho contenedores, incluido PostgreSQL, y depende de la retencion acumulada de los backends. Docker memory usa memoria del contenedor excluyendo cache segun Docker; no es RSS del proceso.',
            'Las muestras de recursos incluyen el drenaje final de hasta 15 s. El p99 corresponde a las peticiones completadas por k6.',
            'Una sola pareja es exploratoria. Para mayor confianza repetir con --repetitions 3; el orden se alterna. La mediana de p99 por corrida no es un p99 combinado.',
            'El generador, los servicios y los backends comparten equipo. No extrapolar estos resultados a GKE/ECS.']
    (root/'comparacion.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('\n'.join(lines))
    return result

if __name__=='__main__': analyze(Path(sys.argv[1]))
