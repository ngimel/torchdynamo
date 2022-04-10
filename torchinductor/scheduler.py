import collections
import contextlib
import dataclasses
from typing import Any
from typing import Dict

from torchinductor import ir
from torchinductor.dependencies import StarDep
from torchinductor.virtualized import graph


class OutputNode:
    def __init__(self, dep):
        self.unmet_dependencies = {dep}

    def is_reduction(self):
        return False


class SchedulerNode:
    def __init__(self, scheduler: "Scheduler", node: ir.ComputedBuffer, group_fn):
        assert isinstance(node, ir.ComputedBuffer)
        self.scheduler = scheduler
        self.node = node
        self.read_writes = node.get_read_writes()
        self.unmet_dependencies = self.read_writes.reads
        self.prune_deps()
        self._size, self._reindex = ir.SqueezeView.squeezer(node.data.get_size())
        self._reduction_size, self._reduction_reindex = ir.SqueezeView.squeezer(
            node.data.get_reduction_size()
        )
        self.group = (
            group_fn(self._size),
            group_fn(self._reduction_size),
        )
        self.users = None

    def prune_deps(self):
        self.unmet_dependencies = {
            dep
            for dep in self.unmet_dependencies
            if dep.name not in self.scheduler.available_buffer_names
        }

    def can_remove_buffer(self):
        if (
            self.is_reduction()
            and len(self.users) == 1
            and len(self.users[0].unmet_dependencies) == 1
        ):
            user = self.users[0]
            dep = next(iter(user.unmet_dependencies))
            # this will get fused into us, so we don't need to keep the buffer
            return not user.is_reduction() and dep in self.read_writes.writes
        return False

    def mark_fusable(self):
        self.scheduler.fusable_deps.update(self.read_writes.writes)

    def get_name(self):
        return self.node.name

    def get_ranges(self):
        return self._size, self._reduction_size

    def is_reduction(self):
        return bool(self.node.data.get_reduction_type())

    def run(self, vars, reduction_vars):
        self.scheduler.run_count += 1
        name = self.get_name()
        indexer = self.node.layout.make_indexer()
        if self.is_reduction():
            vars = self._reindex(vars)
            reduction_vars = self._reduction_reindex(reduction_vars)
            self.node.data.store_reduction(name, indexer, vars, reduction_vars)
        else:
            vars = self._reindex([*vars, *reduction_vars])
            self.node.data.store_output(name, indexer, vars)
        self.scheduler.pending_buffer_names.add(self.get_name())


@dataclasses.dataclass
class SchedulerNodeBox:
    """Allow us to invalidate a blocked node"""

    value: SchedulerNode

    def __bool__(self):
        return self.value is not None

    def pop(self) -> SchedulerNode:
        assert self
        val = self.value
        self.value = None
        return val

    def peek(self) -> SchedulerNode:
        return self.value


class BlockedNodes:
    def __init__(self):
        super().__init__()
        self.name_to_nodes = collections.defaultdict(list)
        self.dep_to_nodes = collections.defaultdict(list)

    def add(self, node: SchedulerNode):
        box = SchedulerNodeBox(node)
        for dep in node.unmet_dependencies:
            self.name_to_nodes[dep.name].append(box)
            self.dep_to_nodes[dep].append(box)

    def pop_name(self, name):
        return [x.pop() for x in self.name_to_nodes.pop(name, []) if x]

    def pop_fusable(self, deps, group):
        assert isinstance(deps, set)
        result = []
        for dep in deps:
            self.dep_to_nodes[dep] = [x for x in self.dep_to_nodes[dep] if x]
            for box in self.dep_to_nodes[dep]:
                if (
                    len(box.peek().unmet_dependencies - deps) == 0
                    and box.peek().group == group
                ):
                    result.append(box.pop())
        return result


class Scheduler:
    def __init__(self, group_fn, nodes):
        super(Scheduler, self).__init__()
        self.group_fn = group_fn
        self.runable_reduction_groups = collections.defaultdict(int)
        self.runable_pointwise_groups = collections.defaultdict(int)
        self.runable_nodes: Dict[Any, SchedulerNode] = collections.defaultdict(list)
        self.blocked_nodes = BlockedNodes()
        self.run_count = 0
        self.nodes = []
        self.kernels = []
        self.available_buffer_names = set(graph.graph_inputs.keys())
        self.pending_buffer_names = set()
        self.fusable_deps = set()
        for node in nodes:
            self.nodes.append(SchedulerNode(self, node, self.group_fn))
        self.compute_users()
        self.enqueue(self.nodes)

    def compute_users(self):
        name_to_users = collections.defaultdict(list)
        for name in graph.graph_outputs:
            name_to_users[name].append(OutputNode(StarDep(name)))
        for node in reversed(self.nodes):
            node.users = name_to_users[node.get_name()]
            name_to_users[node.get_name()] = None
            for read in node.read_writes.reads:
                name_to_users[read.name].append(node)

        updated_nodes = []
        for node in self.nodes:
            if node.users:
                updated_nodes.append(node)
            else:
                # dead code
                graph.removed_buffers.add(node.get_name())
        self.nodes = updated_nodes

    def maybe_remove_buffer(self, node: SchedulerNode):
        if node.can_remove_buffer():
            graph.removed_buffers.add(node.get_name())

    def enqueue(self, node):
        if isinstance(node, (tuple, list)):
            for n in node:
                self.enqueue(n)
            return

        assert isinstance(node, SchedulerNode)
        if node.unmet_dependencies:
            self.blocked_nodes.add(node)
        else:
            self.runable_nodes[node.group].append(node)
            if node.is_reduction():
                # Do reductions first as they yield more possible fusions
                self.runable_reduction_groups[node.group] += 1
            else:
                self.runable_pointwise_groups[node.group] += 1

    def barrier(self):
        """
        Mark all pending_buffer_names as available and enqueue any nodes
        that became runable.
        """
        self.available_buffer_names.update(self.pending_buffer_names)
        nodes_to_add = []
        for name in self.pending_buffer_names:
            for node in self.blocked_nodes.pop_name(name):
                node.prune_deps()
                nodes_to_add.append(node)
        self.pending_buffer_names.clear()
        self.enqueue(nodes_to_add)

    def kernel(self, kernel):
        self.fusable_deps.clear()
        self.kernels.append(kernel)

        @contextlib.contextmanager
        def ctx():
            with kernel:
                yield kernel

        return ctx()

    def iter_runable_groups(self):
        while self.runable_reduction_groups or self.runable_pointwise_groups:
            if self.runable_reduction_groups:
                yield next(iter(self.runable_reduction_groups.keys()))
            else:
                yield next(iter(self.runable_pointwise_groups.keys()))
        assert not self.runable_nodes
        assert len(self.nodes) == self.run_count

    def iter_fixed_point(self):
        """
        Keep yielding until self.run_count converges
        """
        prior_run_count = -1
        while prior_run_count != self.run_count:
            prior_run_count = self.run_count
            yield

    def pop_group(self, group):
        while group in self.runable_nodes:
            self.runable_reduction_groups.pop(group, None)
            self.runable_pointwise_groups.pop(group, None)
            yield from self.runable_nodes.pop(group)
        if self.fusable_deps:
            fusable = True
            while fusable:
                fusable = self.blocked_nodes.pop_fusable(self.fusable_deps, group)
                yield from fusable