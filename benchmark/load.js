import http from 'k6/http';
import { check, sleep, randomSeed } from 'k6';

randomSeed(20261011);
export const options = {
  scenarios: { orders: {
    executor: 'constant-vus', vus: Number(__ENV.VUS || 50),
    duration: __ENV.DURATION || '5m', gracefulStop: '15s',
  } },
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
  thresholds: { http_req_failed: ['rate<0.01'], checks: ['rate>0.99'] },
};

export default function () {
  const body = JSON.stringify({
    customer_id: `benchmark-${__VU}`, amount: 25000 + (__ITER % 10) * 5000, currency: 'COP',
  });
  const res = http.post(`${__ENV.BASE_URL || 'http://127.0.0.1:18000'}/orders`, body, {
    headers: { 'Content-Type': 'application/json' }, timeout: '10s',
    tags: { name: 'POST /orders' },
  });
  check(res, { 'order is PAID': r => {
    try { return r.status === 200 && r.json('status') === 'PAID'; } catch (_) { return false; }
  } });
  // Same reproducible think-time distribution in both arms: 0.5 to 1.5 s.
  sleep(0.5 + Math.random());
}

export function handleSummary(data) {
  return {
    [__ENV.SUMMARY_PATH || 'summary.json']: JSON.stringify(data, null, 2),
    stdout: `\nResumen guardado en ${__ENV.SUMMARY_PATH || 'summary.json'}\n`,
  };
}
