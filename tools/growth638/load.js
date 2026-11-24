import http from 'k6/http';
import { Counter } from 'k6/metrics';
import { scenario } from 'k6/execution';

const fixture = JSON.parse(open('/work/fixture.json'));
const mode = __ENV.MODE;
const rates = JSON.parse(__ENV.RATES || '{}');
const optionsFor = (name, rate, index) => ({
  executor: 'constant-arrival-rate', rate, timeUnit: '1m', duration: __ENV.DURATION || '1m',
  preAllocatedVUs: 12, maxVUs: 50, exec: name, startTime: `${index * 100}ms`,
});
export const options = {
  discardResponseBodies: true,
  scenarios: mode === 'burst' ? {
    growth: { executor: 'per-vu-iterations', vus: 50, iterations: 1, exec: 'growth', maxDuration: '25s' },
  } : Object.fromEntries(Object.entries(rates).map(([name, rate], index) => [name, optionsFor(name, rate, index)])),
};
const started = new Counter('bench_started');
const completed = new Counter('bench_completed');
const timeout = new Counter('bench_timeout');

function send(name, method, path, template) {
  const number = scenario.iterationInTest;
  const token = fixture.tokens[number % fixture.tokens.length];
  const body = template && JSON.stringify({
    ...template, clientUuid: `63800000-0000-4000-${name === 'meal' ? '8001' : '8002'}-${String(number).padStart(12, '0')}`,
  });
  const rid = { api: name };
  started.add(1, rid);
  const response = http.request(method, 'http://app:8080/api/v1' + path, body, {
    headers: { Authorization: 'Bearer ' + token, 'X-Synthetic-Traffic': '1', 'Content-Type': 'application/json' },
    tags: rid, timeout: '15s', redirects: 0,
  });
  completed.add(1, { ...rid, status: String(response.status) });
  timeout.add(response.error_code === 1050 ? 1 : 0, rid);
}
export function growth() { send('growth', 'GET', '/growth/me', null); }
export function meal() { send('meal', 'POST', '/meals', fixture.meal); }
export function workout() { send('workout', 'POST', '/workout-sessions', fixture.workout); }
