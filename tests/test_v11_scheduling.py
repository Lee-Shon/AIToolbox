"""Bounded regressions for V11's two distinct scheduling decisions."""
from copy import deepcopy
import base64
import json
import threading
import time
from types import SimpleNamespace
import unittest

from local_product.scheduling import Placement
from local_product.service import SharedContext
import test_local_product_service as fixtures


class SchedulingTests(unittest.TestCase):
    def test_fresh_multimodal_probe_keeps_native_placement_evidence(self):
        fixture = fixtures.Regressions()
        fixture.setUp()
        try:
            proof = {'input_tokens': 5, 'resource_profile': {'source': 'native-profile'}}
            answer = {'choices': [{'message': {'content': 'AIT V11 OCR 7319 blue sky stars'}}],
                      'native_preparation': proof}
            fixture.client.chat = lambda *args, **kwargs: answer
            fixture.client.transcribe = lambda *args, **kwargs: answer
            for cap in ('image_input', 'audio_input'):
                row = dict(localai_name='fresh', backend='cuda13-vllm', max_context_tokens=4096,
                           capabilities=[cap, 'text_output'])
                checked = fixture.product._probe(row)
                self.assertEqual(checked[cap].get('native_preparation'), proof)
        finally:
            fixture.tearDown()

    def test_explicit_registration_retry_after_gpu_observer_recovers(self):
        fixture = fixtures.Regressions()
        fixture.setUp()
        try:
            product = fixture.product
            snapshot = fixture.client.resource_snapshot
            def unavailable():
                raise fixtures.service.ProductError('physical_resource_observation_unknown', 503)
            fixture.client.resource_snapshot = unavailable
            product.runtime.placement.last_observation = None
            spec = dict(fixture.spec, id='retry-fresh')
            product.register(spec)
            self.assertEqual(product.get('retry-fresh')['state'], 'REJECTED')
            fixture.client.resource_snapshot = snapshot
            status, _ = product.register(spec)
            self.assertEqual(status, 202)
            self.assertEqual(product.get('retry-fresh')['state'], 'READY')
            self.assertEqual(product.get('retry-fresh')['revision'], 1)
        finally:
            fixture.tearDown()

    def test_failed_batch_worker_start_leaves_no_placement_or_request_owners(self):
        fixture = fixtures.Regressions()
        fixture.setUp()
        try:
            product = fixture.product
            def fail_launch(*_):
                raise RuntimeError('cannot start worker')
            product._launch = fail_launch
            body = dict(object_id='object', batch_id='launch-failure', requests=[dict(
                request_id='unlaunched', body=dict(model='model', context_tokens=2000,
                    max_tokens=16, messages=[dict(role='user', content='hello')]))])
            with self.assertRaisesRegex(RuntimeError, 'cannot start worker'):
                product.submit_batch(body)
            self.assertEqual(product.request_counts, {})
            self.assertEqual(product.runtime.placement.pending, [])
            self.assertEqual(product.runtime.placement.instances, [])
            self.assertEqual(product.active_requests, {})
        finally:
            fixture.tearDown()

    def test_changed_configuration_releases_preassigned_batch_ticket(self):
        fixture = fixtures.Regressions()
        fixture.setUp()
        try:
            runtime = fixture.product.runtime
            row = fixture.product.rows['model']
            ticket = runtime.placement.enqueue(row, 2000)
            with runtime.placement.condition:
                runtime.placement.drain()
            self.assertEqual(len(runtime.placement.instances), 1)
            fixture.client.configs['model']['max_context_tokens'] = 9999
            with self.assertRaisesRegex(fixtures.service.ProductError, 'localai_configuration_changed'):
                runtime.acquire(row, 2000, ticket=ticket)
            self.assertEqual(runtime.placement.pending, [])
            self.assertEqual(runtime.placement.instances, [])
        finally:
            fixture.tearDown()

    def test_second_worker_start_failure_cannot_launch_or_double_release_first(self):
        fixture = fixtures.Regressions()
        fixture.setUp()
        workers = []
        try:
            product = fixture.product
            def launch(target, args):
                if workers:
                    raise RuntimeError('second worker failed')
                worker = threading.Thread(target=target, args=args, daemon=True)
                workers.append(worker)
                worker.start()
            product._launch = launch
            body = dict(object_id='object', batch_id='second-worker', requests=[dict(
                request_id='launch-'+str(n), body=dict(model='model', context_tokens=2000,
                    max_tokens=16, messages=[dict(role='user', content='hello')])) for n in range(2)])
            with fixture.backend() as backend:
                with self.assertRaisesRegex(RuntimeError, 'second worker failed'):
                    product.submit_batch(body)
                for worker in workers:
                    worker.join(3)
                self.assertFalse(any(worker.is_alive() for worker in workers))
                self.assertEqual(backend.calls, [])
            self.assertEqual(product.request_counts, {})
            self.assertEqual(product.runtime.placement.pending, [])
            self.assertEqual(product.runtime.placement.instances, [])
            self.assertEqual(product.active_requests, {})
            receipts = [json.loads(p.read_text()) for p in (product.root/'requests').glob('*.json')]
            self.assertEqual([r['state'] for r in receipts], ['FAILED','FAILED'])
        finally:
            fixture.tearDown()

    def test_unavailable_budget_does_not_abort_unrelated_model(self):
        placement, _, _ = self.placement()
        budget = placement.client.resource_budget
        def forecast(row, observation):
            if row['id'] == 'bad':
                raise fixtures.service.ProductError('native_memory_budget_unknown', 503)
            return budget(row, observation)
        placement.client.resource_budget = forecast
        bad = placement.enqueue(self.row('bad', 40), 30)
        good = placement.enqueue(self.row('good', 20), 30)
        with placement.condition:
            placement.drain()
        self.assertIsNotNone(good['instance'])
        self.assertEqual(bad['error'].code, 'native_memory_budget_unknown')

    def row(self, name, budget):
        return dict(id=name, revision=1, localai_hash=name, localai_name=name,
                    max_context_tokens=100, spec={'budget': budget})

    def placement(self):
        stopped, imported = [], []
        client = SimpleNamespace(resource_snapshot=lambda: dict(total_bytes=100,
            used_bytes=0, observed_at=time.time()),
            resource_budget=lambda row, observation: dict(gpu_bytes=row['spec']['budget'],
                total_context_tokens=100, precision='BF16'),
            import_model=lambda name, spec: imported.append(name),
            clone_model=lambda name, row: imported.append(name),
            shutdown=stopped.append, retire=lambda name: None)
        return Placement(client), stopped, imported

    def test_descending_scan_skips_oversized_head_and_reaches_small_tail(self):
        placement, _, _ = self.placement()
        placement.client.resource_snapshot = lambda: dict(total_bytes=100, used_bytes=30, observed_at=time.time())
        large = placement.enqueue(self.row('large', 80), 30, 1)
        small = placement.enqueue(self.row('small', 20), 30, 3)
        medium = placement.enqueue(self.row('medium', 40), 30, 2)
        with placement.condition:
            placement.drain()
        self.assertIsNone(large['instance'])
        self.assertEqual([i.row['id'] for i in placement.instances], ['medium', 'small'])
        self.assertEqual([e['action'] for e in placement.events], ['skip_no_fit', 'place', 'place'])
        self.assertEqual([i.pool.total for i in placement.instances], [100, 100])
        self.assertEqual([i.budget['precision'] for i in placement.instances], ['BF16', 'BF16'])

    def test_reuse_then_replicate_only_when_full_and_space_exists(self):
        placement, stopped, imported = self.placement()
        first = placement.enqueue(self.row('A', 20), 60, 1)
        second = placement.enqueue(self.row('A', 20), 30, 2)
        third = placement.enqueue(self.row('A', 20), 60, 3)
        with placement.condition:
            placement.drain()
        self.assertIs(first['instance'], second['instance'])
        self.assertIsNot(first['instance'], third['instance'])
        self.assertEqual(len(imported), 1)
        placement.release(first['instance'], 60)
        self.assertFalse(stopped)
        placement.release(second['instance'], 30)
        self.assertEqual(stopped, ['A'])

    def test_oldest_feasible_scan_backfills_later_small_request(self):
        pool = SharedContext(8192)
        pool.acquire(5000, 1)
        large, small = threading.Event(), threading.Event()
        def run(tokens, order, signal):
            pool.acquire(tokens, order)
            signal.set()
        oldest = threading.Thread(target=run, args=(4000, 2, large))
        oldest.start()
        deadline = time.time() + 2
        while not pool.waiters and time.time() < deadline:
            time.sleep(.01)
        later = threading.Thread(target=run, args=(2000, 3, small))
        later.start()
        self.assertTrue(small.wait(2))
        self.assertFalse(large.is_set())
        self.assertEqual(pool.tokens, 7000)
        pool.release(2000)
        pool.release(5000)
        self.assertTrue(large.wait(2))
        oldest.join(2); later.join(2)
        pool.release(4000)
        self.assertEqual(pool.tokens, 0)

    def test_slow_preparation_cannot_move_a_later_fitting_request_ahead(self):
        pool = SharedContext(8192)
        pool.acquire(5000, 1)
        slow = pool.enqueue(2000, 2)
        later = pool.enqueue(2000, 3)
        entered = threading.Event()
        worker = threading.Thread(target=lambda: (pool.acquire(2000, ticket=later), entered.set()))
        worker.start()
        time.sleep(.05)
        self.assertFalse(entered.is_set())
        self.assertEqual(pool.tokens, 7000)
        pool.acquire(2000, ticket=slow)
        pool.release(2000)
        self.assertTrue(entered.wait(2))
        worker.join(2)
        pool.release(2000); pool.release(5000)

    def test_resource_profile_does_not_shrink_after_a_smaller_observation(self):
        placement, _, _ = self.placement()
        ticket = placement.enqueue(self.row('A', 20), 30)
        with placement.condition: placement.drain()
        instance = ticket['instance']
        profile = dict(total_context_tokens=100, precision='BF16', resident_gpu_bytes=30, load_gpu_bytes=40)
        placement.observe_profile(instance, profile)
        placement.observe_profile(instance, dict(profile, resident_gpu_bytes=25, load_gpu_bytes=35))
        self.assertEqual(placement.profiles[instance.binding]['load_gpu_bytes'], 40)
        self.assertEqual(instance.budget['gpu_bytes'], 30)

    def test_native_segment_failure_only_targets_original_segment(self):
        fixture = fixtures.Regressions()
        fixture.setUp()
        try:
            product = fixture.product
            product.batches['batch'] = {'failed_segments': set()}
            origin = dict(request_id='A1', association={'key': 'batch', 'segment': 0})
            stopped = []
            product.client.execution_control = lambda model, request_id, phase: (
                stopped.append(request_id) or {'stop_confirmed': True, 'execution_started': True})
            for key, batch, segment, terminal in [('A2', 'batch', 0, False),
                ('done', 'batch', 0, True), ('B1', 'batch', 1, False),
                ('A3', 'batch', 2, False), ('other', 'otherbatch', 0, False)]:
                product.active_requests[key] = dict(association={'key': batch, 'segment': segment},
                    terminal=terminal, cancel=threading.Event(), prepared=True,
                    instance=SimpleNamespace(name='native-A'))
            product.fail_segment(origin)
            self.assertEqual(stopped, ['A2'])
            self.assertTrue(product.active_requests['A2']['cancel'].is_set())
            self.assertTrue(product.active_requests['A2']['stop_proof']['stop_confirmed'])
            self.assertFalse(any(product.active_requests[key]['cancel'].is_set()
                                 for key in ('done', 'B1', 'A3', 'other')))
        finally:
            fixture.tearDown()

    def test_batch_manifest_freezes_segments_and_replay_never_generates_again(self):
        fixture = fixtures.Regressions()
        fixture.setUp()
        try:
            product = fixture.product
            product._launch = lambda target, args: threading.Thread(target=target, args=args).start()
            spec = deepcopy(fixture.spec); spec['id'] = 'B'
            product._launch = lambda target, args: target(*args)
            product.register(spec)
            workers = []
            def launch(target, args):
                worker = threading.Thread(target=target, args=args)
                workers.append(worker); worker.start()
            product._launch = launch
            product.client.execution_control = lambda *args: {'stop_confirmed': True, 'confirmed_output_hit': False}
            body = dict(object_id='object', batch_id='batch', requests=[dict(request_id=key, body=dict(
                model=model, messages=[dict(role='user', content='hello')], context_tokens=2000,
                max_tokens=16)) for key, model in [('A1','model'), ('A2','model'), ('B1','B'), ('A3','model')]])
            with fixture.backend() as backend:
                original = json.dumps(body, indent=2).encode()
                view = product.submit_batch(body, original)
                for worker in workers: worker.join(8)
                self.assertFalse(any(worker.is_alive() for worker in workers))
                view = product.batch_view(view['key'])
                self.assertEqual([r['association']['segment'] for r in view['requests']], [0,0,1,2])
                self.assertEqual([r['state'] for r in view['requests']], ['COMPLETED'] * 4)
                self.assertEqual(base64.b64decode(view['requests'][0]['batch_input_base64']), original)
                executions = len(backend.generations)
                product.submit_batch(body)
                self.assertEqual(len(backend.generations), executions)
                placement = product.runtime.placement.events
                model_orders = [event['order'] for event in placement if event.get('instance') == 'model']
                self.assertEqual(model_orders[-3:], [view['requests'][i]['arrival_sequence'] for i in (0,1,3)])
        finally:
            fixture.tearDown()

    def test_batch_intake_lock_cannot_block_placement_cancellation_check(self):
        fixture = fixtures.Regressions()
        fixture.setUp()
        worker = None
        try:
            product = fixture.product
            product._launch = lambda *_: None
            body = dict(object_id='object', batch_id='batch', requests=[dict(
                request_id='queued', body=dict(model='model', context_tokens=2000,
                    max_tokens=16, messages=[dict(role='user', content='hello')]))])
            view = product.submit_batch(body)
            placement = product.runtime.placement
            ticket = placement.pending[0]
            drained = threading.Event()
            def drain():
                with placement.condition:
                    placement.drain()
                drained.set()
            # Reproduce the opposite sides of the former ABBA deadlock without
            # depending on generation speed or a random thread interleaving.
            with product.batch_lock:
                worker = threading.Thread(target=drain, daemon=True)
                worker.start()
                self.assertTrue(drained.wait(2), 'placement waits on batch intake lock')
            product.fail_segment(view['requests'][0])
            self.assertTrue(ticket['cancelled']())
            with self.assertRaises(fixtures.service.ProductError):
                placement.acquire(ticket)
            self.assertEqual(placement.instances, [])
            product.release_request(product.rows['model'])
        finally:
            if worker is not None:
                worker.join(3)
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
