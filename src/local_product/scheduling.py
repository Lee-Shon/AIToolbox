"""Physical placement forecasts for immutable LocalAI bindings.

This is scheduling metadata, not a second model loader or persistent queue.
Unknown model shapes/physical observations are errors, never guessed defaults.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
import struct
import subprocess
import threading
import time

from .service import ProductError, SharedContext

GIB = 1024 ** 3


def gpu_snapshot():
    try:
        raw = subprocess.check_output(['nvidia-smi',
            '--query-gpu=memory.total,memory.used,utilization.gpu',
            '--format=csv,noheader,nounits'], timeout=5, text=True)
        rows = raw.strip().splitlines()
        if len(rows) != 1:
            raise ValueError('single_gpu_observation_required')
        total, used, utilization = map(int, rows[0].split(','))
        return dict(total_bytes=total * 1024 ** 2, used_bytes=used * 1024 ** 2,
                    utilization=utilization, observed_at=time.time())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ProductError('physical_resource_observation_unknown', 503) from exc


def gguf_metadata(path):
    """Read the GGUF directory only; do not load tensors or modify assets."""
    with Path(path).open('rb') as source:
        def number(fmt):
            size = struct.calcsize('<' + fmt)
            raw = source.read(size)
            if len(raw) != size:
                raise ValueError('truncated_gguf_metadata')
            return struct.unpack('<' + fmt, raw)[0]

        def string(keep=True):
            size = number('Q')
            if size > 1024 ** 3:
                raise ValueError('invalid_gguf_string')
            if keep:
                return source.read(size).decode('utf-8')
            source.seek(size, 1)

        def value(kind, keep):
            types = {0:'B', 1:'b', 2:'H', 3:'h', 4:'I', 5:'i', 6:'f', 7:'?',
                     10:'Q', 11:'q', 12:'d'}
            if kind in types:
                return number(types[kind])
            if kind == 8:
                return string(keep)
            if kind == 9:
                element, count = number('I'), number('Q')
                if count > 10000000:
                    raise ValueError('invalid_gguf_array')
                if element in types:
                    source.seek(count * struct.calcsize('<' + types[element]), 1)
                else:
                    for _ in range(count):
                        value(element, False)
                return None
            raise ValueError('unknown_gguf_metadata_type')

        if source.read(4) != b'GGUF' or number('I') not in (2, 3):
            raise ValueError('unsupported_gguf')
        number('Q')
        count = number('Q')
        if count > 100000:
            raise ValueError('invalid_gguf_directory')
        result = {}
        for _ in range(count):
            key = string()
            keep = not key.startswith('tokenizer.')
            item = value(number('I'), keep)
            if keep:
                result[key] = item
        return result


def instance_budget(row, observation):
    spec = row['spec']
    total = row['max_context_tokens']
    tuning = spec['tuning']
    if row['backend'] == 'cuda13-vllm':
        fraction = tuning.get('gpu_memory_utilization')
        if not isinstance(fraction, (int, float)) or not 0 < fraction < 1:
            raise ProductError('native_memory_budget_unknown', 503)
        config = json.loads((Path(spec['model_path']) / 'config.json').read_text('utf-8'))
        dtype = tuning.get('dtype') or config.get('dtype') or config.get('torch_dtype')
        # The native allocator reserves this fraction for the exact dtype/T;
        # its load still has to prove that the full context is supportable.
        return dict(gpu_bytes=int(observation['total_bytes'] * fraction),
                    precision=dtype or 'native_config', total_context_tokens=total,
                    basis='vllm_frozen_gpu_memory_utilization', localai_hash=row['localai_hash'])
    try:
        metadata = gguf_metadata(spec['model_path'])
        architecture = metadata['general.architecture']
        prefix = architecture + '.'
        layers = int(metadata[prefix + 'block_count'])
        embedding = int(metadata[prefix + 'embedding_length'])
        heads = int(metadata[prefix + 'attention.head_count'])
        kv_heads = int(metadata[prefix + 'attention.head_count_kv'])
        key_dim = int(metadata.get(prefix + 'attention.key_length', embedding // heads))
        value_dim = int(metadata.get(prefix + 'attention.value_length', embedding // heads))
        if min(layers, embedding, heads, kv_heads, key_dim, value_dim) <= 0:
            raise ValueError('invalid_native_attention_shape')
        weights = Path(spec['model_path']).stat().st_size
        media = Path(spec['mmproj_path']).stat().st_size if spec.get('mmproj_path') else 0
        kv = layers * kv_heads * (key_dim + value_dim) * 2 * total
        # Explicit full-offload forecast: weights, full F16 KV and workspace.
        # Never reduce context/layers/precision to satisfy a fit decision.
        workspace = max(GIB, (weights + media) // 20)
        return dict(gpu_bytes=weights + media + kv + workspace,
                    weights_bytes=weights, media_bytes=media, kv_bytes=kv,
                    workspace_bytes=workspace, precision=metadata.get('general.file_type'),
                    total_context_tokens=total, basis='gguf_native_shape_full_f16_kv',
                    localai_hash=row['localai_hash'])
    except (KeyError, OSError, ValueError, ZeroDivisionError) as exc:
        raise ProductError('native_memory_budget_unknown', 503) from exc


@dataclass
class Instance:
    row: dict
    name: str
    budget: dict
    pool: SharedContext
    inflight: int = 0
    assigned_tokens: int = 0
    loaded: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def binding(self):
        return (self.row['id'], self.row['revision'], self.row['localai_hash'])


class Placement:
    """One locked, descending feasible scan; keep blocked heads pending."""
    def __init__(self, client):
        self.client = client
        self.condition = threading.Condition(threading.RLock())
        self.instances = []
        self.pending = []
        self.sequence = 0
        self.outside_floor = 0
        self.events = []
        self.last_observation = None
        self.profiles = {}

    def observe_profile(self, instance, profile):
        with self.condition:
            if instance.row.get('backend') == 'cuda12-llama-cpp' and not profile.get('media_forecast_bytes'):
                media = instance.budget.get('media_bytes', 0)
                profile = dict(profile, resident_gpu_bytes=profile['resident_gpu_bytes']+media,
                    load_gpu_bytes=profile['load_gpu_bytes']+media, media_forecast_bytes=media)
            if profile['total_context_tokens'] != instance.pool.total:
                raise ProductError('native_context_changed', 409)
            expected = str(instance.budget['precision']).removeprefix('torch.')
            if str(profile['precision']).removeprefix('torch.') != expected:
                raise ProductError('native_precision_changed', 409)
            if not 0 < profile['resident_gpu_bytes'] <= profile['load_gpu_bytes']:
                raise ProductError('native_resource_profile_invalid', 502)
            binding = instance.binding
            prior = self.profiles.get(binding, {})
            profile = dict(profile, resident_gpu_bytes=max(profile['resident_gpu_bytes'], prior.get('resident_gpu_bytes', 0)),
                           load_gpu_bytes=max(profile['load_gpu_bytes'], prior.get('load_gpu_bytes', 0)))
            self.profiles[binding] = profile
            instance.budget.update(gpu_bytes=max(instance.budget['gpu_bytes'], profile['resident_gpu_bytes']),
                                   load_gpu_bytes=max(instance.budget['gpu_bytes'], profile['load_gpu_bytes']),
                                   resource_profile=profile)
            instance.loaded = True
            self.condition.notify_all()

    def enqueue(self, row, tokens, order=None, cancelled=lambda: False):
        with self.condition:
            self.sequence += 1
            item = dict(row=row, tokens=tokens, order=order or self.sequence,
                        sequence=self.sequence, instance=None, cancelled=cancelled)
            self.pending.append(item)
            return item

    def acquire(self, item):
        with self.condition:
            try:
                while item['instance'] is None:
                    if item['cancelled']():
                        raise ProductError('request_cancelled', 409)
                    self.drain()
                    if item.get('error'):
                        raise item['error']
                    if item['instance'] is None:
                        self.condition.wait(.2)
                if item['cancelled']():
                    raise ProductError('request_cancelled', 409)
                return item['instance']
            except BaseException:
                self.cancel(item)
                raise
            finally:
                if item in self.pending:
                    self.pending.remove(item)

    def cancel(self, item):
        """Return an unclaimed ticket, including any eagerly assigned instance."""
        with self.condition:
            if item not in self.pending:
                return
            self.pending.remove(item)
            instance = item.get('instance')
            if instance is not None:
                instance.pool.cancel(item['pool_ticket'])
                self.release(instance, item['tokens'])
            self.condition.notify_all()

    def drain(self):
        if self.last_observation is None or time.time() - self.last_observation['observed_at'] >= .5:
            self.last_observation = self.client.resource_snapshot()
        observation = self.last_observation
        if time.time() - observation['observed_at'] > 5:
            raise ProductError('physical_resource_observation_stale', 503)
        if not self.instances:
            self.outside_floor = observation['used_bytes']
        committed = sum(instance.budget.get('load_gpu_bytes', instance.budget['gpu_bytes'])
                        if not instance.loaded else instance.budget['gpu_bytes'] for instance in self.instances)
        outside = max(self.outside_floor, observation['used_bytes'] - committed)
        available = int(observation['total_bytes'] * .9) - outside - committed
        candidates = []
        for item in self.pending:
            if item['instance'] is None and not item.get('error') and not item['cancelled']():
                try:
                    budget = self.client.resource_budget(item['row'], observation)
                except (ProductError, OSError, ValueError) as exc:
                    item['error'] = exc if isinstance(exc, ProductError) else ProductError('native_memory_budget_unknown', 503)
                    continue
                binding = (item['row']['id'], item['row']['revision'], item['row']['localai_hash'])
                profile = self.profiles.get(binding)
                if profile and profile.get('observed_gpu_total_bytes', observation['total_bytes']) != observation['total_bytes']:
                    profile = None
                if profile:
                    budget.update(gpu_bytes=max(budget['gpu_bytes'], profile['resident_gpu_bytes']),
                                  load_gpu_bytes=max(budget['gpu_bytes'], profile['load_gpu_bytes']), resource_profile=profile)
                elif item['row'].get('backend') in {'cuda13-vllm', 'cuda12-llama-cpp'}:
                    budget['requires_isolated_profile'] = True
                if budget['gpu_bytes'] > int(observation['total_bytes'] * .9):
                    item['error'] = ProductError('instance_exceeds_device_capacity', 422)
                    self.events.append(dict(at=time.time(), action='reject_exceeds_device',
                        model=item['row']['id'], order=item['order'], budget=budget,
                        available_bytes=available, observation=observation))
                    continue
                candidates.append((item, budget))
        for item, budget in sorted(candidates, key=lambda pair: (-pair[1]['gpu_bytes'], pair[0]['order'])):
            binding = (item['row']['id'], item['row']['revision'], item['row']['localai_hash'])
            peers = [instance for instance in self.instances if instance.binding == binding]
            feasible = [instance for instance in peers
                        if instance.assigned_tokens + item['tokens'] <= instance.pool.total]
            if feasible:
                instance = min(feasible, key=lambda value: value.created_at)
                action = 'reuse'
            elif (budget.get('load_gpu_bytes', budget['gpu_bytes']) <= available
                  and (not budget.get('requires_isolated_profile') or not self.instances)
                  and observation['used_bytes'] < observation['total_bytes'] * .9):
                ordinal = len(peers)
                name = item['row']['localai_name'] if not ordinal else (
                    'ait-v11-replica-' + __import__('hashlib').sha256(
                        repr(binding).encode()).hexdigest()[:20] + '-' + str(ordinal) + '-' + str(self.sequence))
                if ordinal:
                    try:
                        self.client.clone_model(name, item['row'])
                    except ProductError as exc:
                        item['error'] = exc
                        continue
                instance = Instance(item['row'], name, budget, SharedContext(item['row']['max_context_tokens']))
                self.instances.append(instance)
                available -= budget.get('load_gpu_bytes', budget['gpu_bytes'])
                action = 'place'
            elif peers:
                # Cannot add a replica; its existing native pool does the
                # oldest-first feasible scan after preparation.
                instance = min(peers, key=lambda value: value.assigned_tokens)
                action = 'wait_context'
            else:
                self.events.append(dict(at=time.time(), action='skip_no_fit',
                    model=item['row']['id'], order=item['order'], budget=budget,
                    available_bytes=available, observation=observation))
                continue
            instance.inflight += 1
            instance.assigned_tokens += item['tokens']
            item['instance'] = instance
            item['pool_ticket'] = instance.pool.enqueue(item['tokens'], item['order'])
            self.events.append(dict(at=time.time(), action=action, model=item['row']['id'],
                order=item['order'], instance=instance.name, budget=budget,
                available_bytes=available, observation=observation))
        self.condition.notify_all()
        if len(self.events) > 512:
            del self.events[:-512]

    def release(self, instance, tokens):
        with self.condition:
            instance.inflight -= 1
            instance.assigned_tokens -= tokens
            if instance.inflight == 0:
                # Keep ownership on shutdown failure: do not free a budget
                # for a backend whose exit LocalAI has not confirmed.
                self.client.shutdown(instance.name)
                self.instances.remove(instance)
                if instance.name != instance.row['localai_name']:
                    self.client.retire(instance.name)
            self.condition.notify_all()
