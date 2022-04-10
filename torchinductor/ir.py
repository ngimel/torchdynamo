import dataclasses
from functools import partial
from typing import Any
from typing import Callable
from typing import List

import sympy
import torch
from sympy import Expr
from sympy import Integer

from .dependencies import extract_read_writes
from .virtualized import graph
from .virtualized import ops


class IRNode(object):
    pass


@dataclasses.dataclass
class Layout(IRNode):
    device: torch.device
    dtype: torch.dtype
    size: List[Expr]
    stride: List[Expr]
    offset: Expr = Integer(0)

    def make_indexer(self):
        """A closure containing math to read a given element"""

        def indexer(index):
            assert len(index) == len(self.stride)
            return sum(a * b for a, b in zip(index, self.stride)) + self.offset

        return indexer


@dataclasses.dataclass
class FixedLayout(Layout):
    """A Tensor layout we cannot change"""

    pass


class FlexibleLayout(Layout):
    """A Tensor layout we are allowed to change"""

    @staticmethod
    def default_strides(sizes):
        if len(sizes) == 0:
            return []
        reversed_strides = [sympy.Integer(1)]
        for size in reversed(sizes[1:]):
            reversed_strides.append(size * reversed_strides[-1])
        return list(reversed(reversed_strides))

    def __init__(self, device, dtype, size):
        super(FlexibleLayout, self).__init__(
            device, dtype, size, FlexibleLayout.default_strides(size)
        )


@dataclasses.dataclass
class Loops(IRNode):
    ranges: List[Expr]
    inner_fn: Callable

    def get_size(self):
        return self.ranges

    @classmethod
    def create(cls, *args, **kwargs):
        return TensorBox.create(cls(*args, **kwargs))


@dataclasses.dataclass
class TypedLoops(Loops):
    device: torch.device
    dtype: torch.dtype

    def get_dtype(self):
        return self.dtype

    def get_device(self):
        return self.device


class UnrealizedBuffer(TypedLoops):
    def make_loader(self):
        return self.inner_fn

    def get_reduction_size(self):
        return []

    def get_reduction_type(self):
        return None

    def store_output(self, output_name, indexer, vars):
        return ops.store(output_name, indexer(vars), self.inner_fn(vars))


@dataclasses.dataclass
class Reduction(TypedLoops):
    reduction_size: List[Expr]
    reduction_type: str

    def get_reduction_size(self):
        return self.reduction_size

    def get_reduction_type(self):
        return self.reduction_type

    def store_reduction(self, output_name, indexer, vars, reduction_vars):
        return ops.reduction(
            output_name,
            self.dtype,
            self.reduction_type,
            indexer(vars),
            self.inner_fn(vars, reduction_vars),
        )


@dataclasses.dataclass
class BaseView(IRNode):
    data: IRNode

    def get_dtype(self):
        return self.data.get_dtype()

    def get_device(self):
        return self.data.get_device()


@dataclasses.dataclass
class ExpandView(BaseView):
    size: List[Expr]

    def get_size(self):
        return self.size

    def make_loader(self):
        target = self.get_size()
        actual = self.data.get_size()
        skip = len(target) - len(actual)
        inner = self.data.make_loader()

        def load(index):
            index = list(index[skip:])
            assert len(index) == len(actual)
            for i in range(len(actual)):
                if actual[i] == 1:
                    # zero out broadcast dimension
                    index[i] = sympy.Integer(0)
            return inner(index)

        return load


class SqueezeView(BaseView):
    @staticmethod
    def squeezer(size):
        new_size = [s for s in size if s != 1]
        not_one = [i for i, s in enumerate(size) if s != 1]
        length = len(size)

        def reindex(index):
            assert len(index) == len(not_one), f"{index} {not_one}"
            new_index = [sympy.Integer(0)] * length
            for idx, s in zip(not_one, index):
                new_index[idx] = s
            return tuple(new_index)

        return new_size, reindex

    def __init__(self, data):
        super(SqueezeView, self).__init__(data)
        self.size, self.reindex = SqueezeView.squeezer(data.get_size())

    def get_size(self):
        return self.size

    def make_loader(self):
        def load(index):
            return inner(self.reindex(index))

        inner = self.data.make_loader()
        return load


@dataclasses.dataclass
class Constant(IRNode):
    value: Any
    dtype: torch.dtype
    device: torch.device

    def get_size(self):
        return ()

    def get_dtype(self):
        return self.dtype

    def get_device(self):
        return self.device

    def make_loader(self):
        def loader(index):
            return ops.constant(self.value, self.dtype)

        return loader

    def mark_reuse(self, users):
        pass


@dataclasses.dataclass
class Buffer(IRNode):
    name: str
    layout: Layout

    def get_name(self):
        return self.name

    def get_device(self):
        return self.layout.device

    def get_dtype(self):
        return self.layout.dtype

    def get_size(self):
        return self.layout.size

    def get_stride(self):
        return self.layout.stride

    def make_loader(self):
        indexer = self.layout.make_indexer()

        def loader(index):
            return ops.load(self.name, indexer(index))

        return loader


@dataclasses.dataclass
class InputBuffer(Buffer):
    pass


@dataclasses.dataclass
class ComputedBuffer(Buffer):
    data: TypedLoops

    def get_read_writes(self):
        indexer = self.layout.make_indexer()
        if self.data.get_reduction_type():
            return extract_read_writes(
                partial(self.data.store_reduction, self.name, indexer),
                self.data.get_size(),
                self.data.get_reduction_size(),
            )
        else:
            return extract_read_writes(
                partial(self.data.store_output, self.name, indexer),
                self.data.get_size(),
            )


@dataclasses.dataclass
class MutableBox(IRNode):
    """
    TensorBox / StorageBox allow in-place mutation of Tensors
    """

    data: IRNode

    def __getattr__(self, name):
        fn = getattr(self.data, name)
        if callable(fn):
            return fn
        raise AttributeError(f"{type(self.data).__name__}.{name} not callable")


class TensorBox(MutableBox):
    @staticmethod
    def create(data):
        return TensorBox(StorageBox(data))

    def mark_reuse(self, users):
        pass  # TODO(jansel): realize the buffers?


class StorageBox(MutableBox):
    def realize(self):
        if isinstance(self.data, ComputedBuffer):
            return self.data.name
        assert isinstance(self.data, (UnrealizedBuffer, Reduction))
        self.data = ComputedBuffer(
            name=None,
            layout=FlexibleLayout(
                device=self.data.get_device(),
                dtype=self.data.get_dtype(),
                size=self.data.get_size(),
            ),
            data=self.data,
        )
        self.data.name = graph.register_buffer(self.data)
        return self.data.name


@dataclasses.dataclass
class View(IRNode):
    storage: StorageBox