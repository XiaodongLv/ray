import itertools
import pytest
import threading
import time

from typing import List, Any

import ray
from ray import cloudpickle
from ray.data.context import DatasetContext
from ray.data._internal.execution.interfaces import (
    ExecutionOptions,
    ExecutionResources,
    RefBundle,
)
from ray.data._internal.execution.streaming_executor import (
    StreamingExecutor,
)
from ray.data._internal.execution.operators.all_to_all_operator import AllToAllOperator
from ray.data._internal.execution.operators.map_operator import MapOperator
from ray.data._internal.execution.operators.input_data_buffer import InputDataBuffer
from ray.data._internal.execution.operators.output_splitter import OutputSplitter
from ray.data._internal.execution.util import make_ref_bundles
from ray._private.test_utils import wait_for_condition
from ray.data.tests.conftest import *  # noqa


def make_transform(block_fn):
    def map_fn(block_iter, ctx):
        for block in block_iter:
            yield block_fn(block)

    return map_fn


def ref_bundles_to_list(bundles: List[RefBundle]) -> List[List[Any]]:
    output = []
    for bundle in bundles:
        for block, _ in bundle.blocks:
            output.append(ray.get(block))
    return output


def test_pipelined_execution(ray_start_10_cpus_shared):
    executor = StreamingExecutor(ExecutionOptions(preserve_order=True))
    inputs = make_ref_bundles([[x] for x in range(20)])
    o1 = InputDataBuffer(inputs)
    o2 = MapOperator.create(make_transform(lambda block: [b * -1 for b in block]), o1)
    o3 = MapOperator.create(make_transform(lambda block: [b * 2 for b in block]), o2)

    def reverse_sort(inputs: List[RefBundle], ctx):
        reversed_list = inputs[::-1]
        return reversed_list, {}

    o4 = AllToAllOperator(reverse_sort, o3)
    it = executor.execute(o4)
    output = ref_bundles_to_list(it)
    expected = [[x * -2] for x in range(20)][::-1]
    assert output == expected, (output, expected)


def test_output_split_e2e(ray_start_10_cpus_shared):
    executor = StreamingExecutor(ExecutionOptions())
    inputs = make_ref_bundles([[x] for x in range(20)])
    o1 = InputDataBuffer(inputs)
    o2 = OutputSplitter(o1, 2, equal=True)
    it = executor.execute(o2)

    class Consume(threading.Thread):
        def __init__(self, idx):
            self.idx = idx
            self.out = []
            super().__init__()

        def run(self):
            while True:
                try:
                    self.out.append(it.get_next(output_split_idx=self.idx))
                except Exception as e:
                    print(e)
                    raise

    c0 = Consume(0)
    c1 = Consume(1)
    c0.start()
    c1.start()
    c0.join()
    c1.join()
    assert len(c0.out) == 10, c0.out
    assert len(c1.out) == 10, c0.out


def test_streaming_split_e2e(ray_start_10_cpus_shared):
    def get_lengths(*iterators, use_iter_batches=True):
        lengths = []

        class Runner(threading.Thread):
            def __init__(self, it):
                self.it = it
                super().__init__()

            def run(self):
                it = self.it
                x = 0
                if use_iter_batches:
                    for batch in it.iter_batches():
                        x += len(batch)
                else:
                    for _ in it.iter_rows():
                        x += 1
                lengths.append(x)

        runners = [Runner(it) for it in iterators]
        for r in runners:
            r.start()
        for r in runners:
            r.join()

        lengths.sort()
        return lengths

    ds = ray.data.range(1000)
    (
        i1,
        i2,
    ) = ds.streaming_split(2, equal=True)
    for _ in range(2):
        lengths = get_lengths(i1, i2)
        assert lengths == [500, 500], lengths

    ds = ray.data.range(1)
    (
        i1,
        i2,
    ) = ds.streaming_split(2, equal=True)
    for _ in range(2):
        lengths = get_lengths(i1, i2)
        assert lengths == [0, 0], lengths

    ds = ray.data.range(1)
    (
        i1,
        i2,
    ) = ds.streaming_split(2, equal=False)
    for _ in range(2):
        lengths = get_lengths(i1, i2)
        assert lengths == [0, 1], lengths

    ds = ray.data.range(1000, parallelism=10)
    for equal_split, use_iter_batches in itertools.product(
        [True, False], [True, False]
    ):
        i1, i2, i3 = ds.streaming_split(3, equal=equal_split)
        for _ in range(2):
            lengths = get_lengths(i1, i2, i3, use_iter_batches=use_iter_batches)
            if equal_split:
                assert lengths == [333, 333, 333], lengths
            else:
                assert lengths == [300, 300, 400], lengths


def test_streaming_split_barrier(ray_start_10_cpus_shared):
    ds = ray.data.range(20, parallelism=20)
    (
        i1,
        i2,
    ) = ds.streaming_split(2, equal=True)

    @ray.remote
    def consume(x, times):
        i = 0
        for _ in range(times):
            for _ in x.iter_rows():
                i += 1
        return i

    # Succeeds.
    ray.get([consume.remote(i1, 2), consume.remote(i2, 2)])
    ray.get([consume.remote(i1, 2), consume.remote(i2, 2)])
    ray.get([consume.remote(i1, 2), consume.remote(i2, 2)])

    # Blocks forever since one reader is stalled.
    with pytest.raises(ray.exceptions.GetTimeoutError):
        ray.get([consume.remote(i1, 2), consume.remote(i2, 1)], timeout=3)


def test_streaming_split_invalid_iterator(ray_start_10_cpus_shared):
    ds = ray.data.range(20, parallelism=20)
    (
        i1,
        i2,
    ) = ds.streaming_split(2, equal=True)

    @ray.remote
    def consume(x, times):
        i = 0
        for _ in range(times):
            for _ in x.iter_rows():
                i += 1
        return i

    # InvalidIterator error from too many concurrent readers.
    with pytest.raises(ValueError):
        ray.get(
            [
                consume.remote(i1, 4),
                consume.remote(i2, 4),
                consume.remote(i1, 4),
                consume.remote(i2, 4),
            ]
        )


def test_e2e_option_propagation(ray_start_10_cpus_shared, restore_dataset_context):
    DatasetContext.get_current().new_execution_backend = True
    DatasetContext.get_current().use_streaming_executor = True

    def run():
        ray.data.range(5, parallelism=5).map(
            lambda x: x, compute=ray.data.ActorPoolStrategy(2, 2)
        ).take_all()

    DatasetContext.get_current().execution_options.resource_limits = (
        ExecutionResources()
    )
    run()

    DatasetContext.get_current().execution_options.resource_limits.cpu = 1
    with pytest.raises(ValueError):
        run()


def test_configure_spread_e2e(ray_start_10_cpus_shared, restore_dataset_context):
    from ray import remote_function

    tasks = []

    def _test_hook(fn, args, strategy):
        if "map_task" in str(fn):
            tasks.append(strategy)

    remote_function._task_launch_hook = _test_hook
    DatasetContext.get_current().use_streaming_executor = True
    DatasetContext.get_current().execution_options.preserve_order = True

    # Simple 2-stage pipeline.
    ray.data.range(2, parallelism=2).map(lambda x: x, num_cpus=2).take_all()

    # Read tasks get SPREAD by default, subsequent ones use default policy.
    tasks = sorted(tasks)
    assert tasks == ["DEFAULT", "DEFAULT", "SPREAD", "SPREAD"]


def test_scheduling_progress_when_output_blocked(
    ray_start_10_cpus_shared, restore_dataset_context
):
    # Processing stages should fully finish even if output is completely stalled.

    @ray.remote
    class Counter:
        def __init__(self):
            self.i = 0

        def inc(self):
            self.i += 1

        def get(self):
            return self.i

    counter = Counter.remote()

    def func(x):
        ray.get(counter.inc.remote())
        return x

    DatasetContext.get_current().use_streaming_executor = True
    DatasetContext.get_current().execution_options.preserve_order = True

    # Only take the first item from the iterator.
    it = iter(
        ray.data.range(100, parallelism=100)
        .map_batches(func, batch_size=None)
        .iter_batches(batch_size=None)
    )
    next(it)
    # The pipeline should fully execute even when the output iterator is blocked.
    wait_for_condition(lambda: ray.get(counter.get.remote()) == 100)
    # Check we can take the rest.
    assert list(it) == [[x] for x in range(1, 100)]


def test_backpressure_from_output(ray_start_10_cpus_shared, restore_dataset_context):
    # Here we set the memory limit low enough so the output getting blocked will
    # actually stall execution.

    @ray.remote
    class Counter:
        def __init__(self):
            self.i = 0

        def inc(self):
            self.i += 1

        def get(self):
            return self.i

    counter = Counter.remote()

    def func(x):
        ray.get(counter.inc.remote())
        return x

    ctx = DatasetContext.get_current()
    ctx.use_streaming_executor = True
    ctx.execution_options.resource_limits.object_store_memory = 10000

    # Only take the first item from the iterator.
    ds = ray.data.range(100000, parallelism=100).map_batches(func, batch_size=None)
    it = iter(ds.iter_batches(batch_size=None))
    next(it)
    num_finished = ray.get(counter.get.remote())
    assert num_finished < 5, num_finished
    # Check intermediate stats reporting.
    stats = ds.stats()
    assert "100/100 blocks executed" not in stats, stats

    # Check we can get the rest.
    for rest in it:
        pass
    assert ray.get(counter.get.remote()) == 100
    # Check final stats reporting.
    stats = ds.stats()
    assert "100/100 blocks executed" in stats, stats


def test_e2e_liveness_with_output_backpressure_edge_case(
    ray_start_10_cpus_shared, restore_dataset_context
):
    # At least one operator is ensured to be running, if the output becomes idle.
    ctx = DatasetContext.get_current()
    ctx.use_streaming_executor = True
    ctx.execution_options.preserve_order = True
    ctx.execution_options.resource_limits.object_store_memory = 1
    ds = ray.data.range(10000, parallelism=100).map(lambda x: x, num_cpus=2)
    # This will hang forever if the liveness logic is wrong, since the output
    # backpressure will prevent any operators from running at all.
    assert ds.take_all() == list(range(10000))


def test_e2e_autoscaling_up(ray_start_10_cpus_shared, restore_dataset_context):
    DatasetContext.get_current().new_execution_backend = True
    DatasetContext.get_current().use_streaming_executor = True

    @ray.remote(max_concurrency=10)
    class Barrier:
        def __init__(self, n, delay=0):
            self.n = n
            self.delay = delay
            self.max_waiters = 0
            self.cur_waiters = 0

        def wait(self):
            self.cur_waiters += 1
            if self.cur_waiters > self.max_waiters:
                self.max_waiters = self.cur_waiters
            self.n -= 1
            print("wait", self.n)
            while self.n > 0:
                time.sleep(0.1)
            time.sleep(self.delay)
            print("wait done")
            self.cur_waiters -= 1

        def get_max_waiters(self):
            return self.max_waiters

    b1 = Barrier.remote(6)

    def barrier1(x):
        ray.get(b1.wait.remote(), timeout=10)
        return x

    # Tests that we autoscale up to necessary size.
    # 6 tasks + 1 tasks in flight per actor => need at least 6 actors to run.
    ray.data.range(6, parallelism=6).map_batches(
        barrier1,
        compute=ray.data.ActorPoolStrategy(1, 6, max_tasks_in_flight_per_actor=1),
        batch_size=None,
    ).take_all()
    assert ray.get(b1.get_max_waiters.remote()) == 6

    b2 = Barrier.remote(3, delay=2)

    def barrier2(x):
        ray.get(b2.wait.remote(), timeout=10)
        return x

    # Tests that we don't over-scale up.
    # 6 tasks + 2 tasks in flight per actor => only scale up to 3 actors
    ray.data.range(6, parallelism=6).map_batches(
        barrier2,
        compute=ray.data.ActorPoolStrategy(1, 3, max_tasks_in_flight_per_actor=2),
        batch_size=None,
    ).take_all()
    assert ray.get(b2.get_max_waiters.remote()) == 3

    # Tests that the max pool size is respected.
    b3 = Barrier.remote(6)

    def barrier3(x):
        ray.get(b3.wait.remote(), timeout=2)
        return x

    # This will hang, since the actor pool is too small.
    with pytest.raises(ray.exceptions.RayTaskError):
        ray.data.range(6, parallelism=6).map(
            barrier3, compute=ray.data.ActorPoolStrategy(1, 2)
        ).take_all()


def test_e2e_autoscaling_down(ray_start_10_cpus_shared, restore_dataset_context):
    DatasetContext.get_current().new_execution_backend = True
    DatasetContext.get_current().use_streaming_executor = True

    def f(x):
        time.sleep(1)
        return x

    # Tests that autoscaling works even when resource constrained via actor killing.
    # To pass this, we need to autoscale down to free up slots for task execution.
    DatasetContext.get_current().execution_options.resource_limits.cpu = 2
    ray.data.range(5, parallelism=5).map_batches(
        f,
        compute=ray.data.ActorPoolStrategy(1, 2),
        batch_size=None,
    ).map_batches(lambda x: x, batch_size=None, num_cpus=2).take_all()


def test_can_pickle(ray_start_10_cpus_shared, restore_dataset_context):
    DatasetContext.get_current().new_execution_backend = True
    DatasetContext.get_current().use_streaming_executor = True

    ds = ray.data.range(1000000)
    it = ds.iter_batches()
    next(it)

    # Should work even if a streaming exec is in progress.
    ds2 = cloudpickle.loads(cloudpickle.dumps(ds))
    assert ds2.count() == 1000000


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", __file__]))
