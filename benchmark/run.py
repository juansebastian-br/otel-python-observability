"""Isolated local experiment. Python stdlib only; requires Docker Compose and k6."""
import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ['docker', 'compose', '-p', 'otel-juan-jose', '-f', 'docker-compose.yml', '-f', 'compose.juan-jose.yaml']
SERVICES = ['orders-service', 'payments-service', 'otel-collector', 'postgres', 'loki', 'jaeger', 'prometheus', 'grafana']

def command(args, env=None, capture=False):
    return subprocess.run(args, cwd=ROOT, env=env, check=True, text=True,
                          encoding='utf-8', errors='replace', capture_output=capture)

def ready(url, timeout=180):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=3) as res:
                if res.status == 200:
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f'No esta listo: {url}')

def sample(stop, path, ids, errors):
    with path.open('w', newline='', encoding='utf-8') as f:
        out = csv.DictWriter(f, fieldnames=['timestamp_utc', 'service', 'cpu_percent', 'memory_mib'])
        out.writeheader()
        while not stop.is_set():
            try:
                # Docker CPU%: 100% is one logical core, not the whole host.
                data = command(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *ids], capture=True)
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                for line in data.stdout.splitlines():
                    row = json.loads(line)
                    mem = row['MemUsage'].split('/')[0].strip()
                    import re
                    value, unit = re.fullmatch(r'([0-9.]+)\s*([A-Za-z]+)', mem).groups()
                    factor = {'B': 1/1048576, 'KiB': 1/1024, 'MiB': 1, 'GiB': 1024,
                              'kB': 1000/1048576, 'MB': 1e6/1048576, 'GB': 1e9/1048576}[unit]
                    cid = next((x for x in ids if x.startswith(row['ID'])), None)
                    if cid is None:
                        raise ValueError(f'Unknown container: {row}')
                    out.writerow(dict(timestamp_utc=now, service=ids[cid],
                                      cpu_percent=float(row['CPUPerc'].strip('%')),
                                      memory_mib=float(value)*factor))
                f.flush()
            except Exception as exc:
                errors.append(str(exc))
            stop.wait(3)

def load(out, env, vus, seconds, name):
    e = dict(env, VUS=str(vus), DURATION=f'{seconds}s',
             SUMMARY_PATH=str((out / f'{name}.json').resolve()), BASE_URL='http://127.0.0.1:18000')
    with (out / f'{name}.log').open('w', encoding='utf-8') as log:
        p = subprocess.run(['k6', 'run', '--quiet', 'benchmark/load.js'], cwd=ROOT, env=e,
                           stdout=log, stderr=subprocess.STDOUT)
    return p.returncode

def run(args):
    command(['docker', 'info'], capture=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = ROOT / 'benchmark/results' / stamp
    out.mkdir(parents=True)
    (ROOT / 'benchmark/results/LATEST.txt').write_text(stamp)
    env = dict(os.environ, OTEL_SDK_DISABLED='false')
    meta = {'environment': 'local Docker Desktop', 'utc_started': stamp,
            'host': platform.platform(), 'logical_cpus_host': os.cpu_count(), 'vus': args.vus,
            'duration_seconds': args.seconds, 'warmup_seconds': args.warmup, 'repetitions': args.repetitions,
            'sampling': 'always_on', 'payments_failure_rate': 0, 'order': [],
            'git_base': command(['git', 'rev-parse', 'HEAD'], capture=True).stdout.strip(),
            'git_diff': command(['git', 'diff', '--stat'], capture=True).stdout,
            'docker': command(['docker', 'version', '--format', '{{json .}}'], capture=True).stdout,
            'k6': command(['k6', 'version'], capture=True).stdout}
    (out/'metadata.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    print(f'Resultados: {out}', flush=True)
    command(COMPOSE + ['up', '-d', '--build'], env=env)
    for port, endpoint in [(18000, '/health'), (18001, '/health'), (13100, '/ready'), (13000, '/api/health'), (13134, '/')]:
        ready(f'http://127.0.0.1:{port}{endpoint}')
    command([sys.executable, 'benchmark/verify_correlation.py', '--output', str(out/'correlation')])
    all_ok = True
    try:
        for rep in range(1, args.repetitions + 1):
            modes = ['baseline', 'otel'] if rep % 2 else ['otel', 'baseline']
            for mode in modes:
                name = f'{rep:02d}-{mode}'
                arm = out/name
                arm.mkdir()
                print(f'{name}: calentamiento {args.warmup}s + medicion {args.seconds}s ({args.vus} VUs)', flush=True)
                e = dict(env, OTEL_SDK_DISABLED=str(mode == 'baseline').lower())
                command(COMPOSE + ['stop', 'orders-service', 'payments-service'])
                # Reset only the dedicated benchmark project's database.
                command(COMPOSE + ['exec', '-T', 'postgres', 'psql', '-U', 'otel', '-d', 'observability', '-c',
                                   'TRUNCATE orders, payments RESTART IDENTITY;'])
                command(COMPOSE + ['up', '-d', '--no-deps', '--force-recreate', 'orders-service', 'payments-service'], env=e)
                ready('http://127.0.0.1:18000/health'); ready('http://127.0.0.1:18001/health')
                # Cooldown makes delayed exporter batches from the previous arm drain.
                time.sleep(15)
                if load(arm, e, args.vus, args.warmup, 'warmup') != 0:
                    raise RuntimeError(f'Calentamiento fallido: {arm / "warmup.log"}')
                ids = {command(COMPOSE+['ps','-q',svc], capture=True).stdout.strip():svc for svc in SERVICES}
                if '' in ids: raise RuntimeError('Un contenedor no esta activo')
                errors = []
                stop = threading.Event()
                worker = threading.Thread(target=sample, args=(stop, arm/'resources.csv', ids, errors))
                started = dt.datetime.now(dt.timezone.utc).isoformat()
                worker.start()
                try:
                    code = load(arm, e, args.vus, args.seconds, 'summary')
                finally:
                    stop.set(); worker.join(timeout=30)
                state = {'mode': mode, 'started_utc': started, 'ended_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                         'k6_exit_code': code, 'sampling_errors': errors, 'resource_sampling_includes_graceful_stop': True}
                (arm/'run.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
                logs = command(COMPOSE+['logs','--no-color','--tail','200','otel-collector'], capture=True)
                (arm/'collector.log').write_text(logs.stdout + logs.stderr, encoding='utf-8')
                meta['order'].append(name)
                (out/'metadata.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
                all_ok &= code == 0 and not errors
    finally:
        # Restore instrumented services so Explore and Jaeger remain usable.
        command(COMPOSE+['up','-d','--no-deps','--force-recreate','orders-service','payments-service'], env=env)
    command([sys.executable, 'benchmark/analyze.py', str(out)])
    ready('http://127.0.0.1:18000/health')
    ready('http://127.0.0.1:18001/health')
    command([sys.executable, 'benchmark/verify_correlation.py', '--output', str(out/'correlation-final')])
    command([sys.executable, 'benchmark/verify_grafana_api.py', '--output', str(out/'correlation-final')])
    print('Terminado. Grafana: http://localhost:13000 | Jaeger: http://localhost:16687', flush=True)
    if not all_ok: raise SystemExit('Hay errores de carga o muestreo; revisar resultados antes de concluir.')

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--vus', type=int, default=50, choices=range(50,101))
    p.add_argument('--seconds', type=int, default=300)
    p.add_argument('--warmup', type=int, default=30)
    p.add_argument('--repetitions', type=int, default=1)
    a = p.parse_args()
    if a.seconds < 300 or a.warmup < 1 or a.repetitions < 1: p.error('Minimo 300 s, warmup positivo y una repeticion')
    run(a)
