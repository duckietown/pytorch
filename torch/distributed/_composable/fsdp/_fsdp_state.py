import functools

from typing import Any, Dict, List, Optional, Tuple

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.autograd.graph import register_multi_grad_hook

from torch.distributed._composable_state import (
    _get_module_state,
    _insert_module_state,
    _State,
)
from torch.distributed.utils import _to_kwargs
from torch.utils._pytree import tree_flatten, tree_map
from torch.utils.hooks import RemovableHandle
from ._fsdp_api import MixedPrecisionPolicy
from ._fsdp_collectives import AllGatherStateHolder
from ._fsdp_common import _cast_floating_point_tensor, TrainingState
from ._fsdp_param import FSDPParam
from ._fsdp_param_group import FSDPParamGroup


class FSDPState(_State):
    _module: nn.Module  # permit ref cycle since module and state lifetimes are 1:1
    _device: torch.device
    _mp_policy: MixedPrecisionPolicy
    _default_stream: torch.cuda.Stream
    _all_gather_copy_in_stream: torch.cuda.Stream
    _all_gather_stream: torch.cuda.Stream
    _reduce_scatter_stream: torch.cuda.Stream
    # For overlapping current copy-out and next all-gather in forward
    _all_gather_state: AllGatherStateHolder

    def __init__(self):
        super().__init__()
        self._fsdp_param_group: Optional[FSDPParamGroup] = None
        self._is_root: Optional[bool] = None
        self._training_state: TrainingState = TrainingState.IDLE
        self._pre_forward_hook_handle: Optional[RemovableHandle] = None
        self._pre_backward_hook_handles: List[RemovableHandle] = []
        # Shared post-forward order for explicit backward prefetching
        self._post_forward_order: List[FSDPParamGroup] = []  # will cause ref cycles

        # Attributes only used on the root state:
        self._all_states: List[FSDPState] = []
        self._root_post_backward_final_callback_queued: Optional[bool] = None

    # Define a separate init since `__init__` is called in the contract
    def init(
        self, module: nn.Module, device: torch.device, mp_policy: MixedPrecisionPolicy
    ) -> None:
        _insert_module_state(module, self)
        self._module = module
        self._device = device
        self._mp_policy = mp_policy
        self._pre_forward_hook_handle = self._module.register_forward_pre_hook(
            self._pre_forward, prepend=True, with_kwargs=True
        )
        self._post_forward_hook_handle = self._module.register_forward_hook(
            self._post_forward, prepend=False
        )

    def _root_pre_forward(
        self, module: nn.Module, args: Tuple[Any, ...], kwargs: Dict[str, Any]
    ) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        self._lazy_init()
        if not self._is_root:
            return args, kwargs
        with torch.profiler.record_function("FSDP::root_pre_forward"):
            # Wait for optimizer before implicitly prefetched all-gathers
            current_stream = torch.cuda.current_stream()
            self._all_gather_copy_in_stream.wait_stream(current_stream)
            self._all_gather_stream.wait_stream(current_stream)
            if self._device.type == "cuda":
                with torch.profiler.record_function("FSDP::inputs_to_device"):
                    args_tuple, kwargs_tuple = _to_kwargs(
                        args, kwargs, self._device, False
                    )  # same as DDP
                args, kwargs = args_tuple[0], kwargs_tuple[0]
        return args, kwargs

    def _lazy_init(self) -> None:
        if self._is_root is not None:
            return  # no-op: already initialized
        self._is_root = True
        root_module = self._module
        for module in root_module.modules():
            if (state := _get_module_fsdp_state(module)) is not None:
                if module is not root_module:
                    state._is_root = False
                self._all_states.append(state)
        if self._fsdp_param_group:
            # For the root, do not reshard after forward since for training,
            # the parameters would be freed and all-gathered immediately
            self._fsdp_param_group.post_forward_mesh_info = None
        self._init_fqns()
        self._init_shared_state()

    def _init_shared_state(self) -> None:
        # Setting the all-gather/reduce-scatter streams to be higher priority
        # can help avoid some issues where their copies in/out are delayed and
        # block computation
        high_priority = -1
        self._default_stream = torch.cuda.current_stream()
        self._all_gather_copy_in_stream = torch.cuda.Stream(priority=high_priority)
        self._all_gather_stream = torch.cuda.Stream(priority=high_priority)
        self._reduce_scatter_stream = torch.cuda.Stream(priority=high_priority)
        self._all_gather_state = AllGatherStateHolder()
        for state in self._all_states:
            if fsdp_param_group := state._fsdp_param_group:
                fsdp_param_group.default_stream = self._default_stream
                fsdp_param_group.all_gather_copy_in_stream = (
                    self._all_gather_copy_in_stream
                )
                fsdp_param_group.all_gather_stream = self._all_gather_stream
                fsdp_param_group.reduce_scatter_stream = self._reduce_scatter_stream
                fsdp_param_group.all_gather_state = self._all_gather_state
                fsdp_param_group.post_forward_order = self._post_forward_order

    def _init_fqns(self) -> None:
        """Sets module and parameter FQN attributes for debugging."""
        assert self._is_root
        root_module = self._module
        param_to_fsdp_param: Dict[nn.Parameter, FSDPParam] = {}
        module_to_fsdp_param_group: Dict[nn.Module, FSDPParamGroup] = {}
        for state in self._all_states:
            if fsdp_param_group := state._fsdp_param_group:
                for fsdp_param in fsdp_param_group.fsdp_params:
                    param_to_fsdp_param[fsdp_param.sharded_param] = fsdp_param
                module_to_fsdp_param_group[fsdp_param_group.module] = fsdp_param_group
        for param_name, param in root_module.named_parameters():
            if param in param_to_fsdp_param:
                param_to_fsdp_param[param]._param_fqn = param_name
        for module_name, module in root_module.named_modules():
            if module in module_to_fsdp_param_group:
                module_to_fsdp_param_group[module]._module_fqn = module_name

    def _pre_forward(
        self, module: nn.Module, args: Tuple[Any, ...], kwargs: Dict[str, Any]
    ) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        # When composing with module-hook-based activation checkpointing, the
        # the pre-backward hook is responsible for the unshard
        if self._training_state == TrainingState.PRE_BACKWARD:
            return args, kwargs
        self._training_state = TrainingState.FORWARD
        args, kwargs = self._root_pre_forward(module, args, kwargs)
        if self._mp_policy.cast_forward_inputs and self._mp_policy.param_dtype:
            with torch.profiler.record_function("FSDP::cast_forward_inputs"):
                cast_fn = functools.partial(
                    _cast_floating_point_tensor, self._mp_policy.param_dtype
                )
                args, kwargs = tree_map(cast_fn, args), tree_map(cast_fn, kwargs)
        if self._fsdp_param_group:
            args, kwargs = self._fsdp_param_group.pre_forward(module, args, kwargs)
        return args, kwargs

    def _post_forward(self, module: nn.Module, input: Any, output: Any) -> Any:
        # When composing with module-hook-based activation checkpointing, the
        # post-backward hook is responsible for the reshard
        if self._training_state == TrainingState.PRE_BACKWARD:
            return output
        if self._fsdp_param_group:
            output = self._fsdp_param_group.post_forward(module, input, output)
        output = self._register_pre_backward_hook(output)
        self._training_state = TrainingState.IDLE
        if self._is_root and (all_gather_state := self._all_gather_state.pop()):
            self._all_gather_copy_in_stream.wait_event(all_gather_state.event)
            self._all_gather_stream.wait_event(all_gather_state.event)
            del all_gather_state  # free
        if self._mp_policy.output_dtype is not None:
            with torch.profiler.record_function("FSDP::cast_forward_outputs"):
                output = tree_map(
                    functools.partial(
                        _cast_floating_point_tensor, self._mp_policy.output_dtype
                    ),
                    output,
                )
        return output

    def _pre_backward(self, *unused: Any) -> None:
        self._training_state = TrainingState.PRE_BACKWARD
        if self._is_root and not self._root_post_backward_final_callback_queued:
            self._register_root_post_backward_final_callback()
        if self._fsdp_param_group:
            self._fsdp_param_group.pre_backward(*unused)

    def _root_post_backward_final_callback(self) -> None:
        if not self._is_root:
            return
        with torch.profiler.record_function("FSDP::root_post_backward_callback"):
            self._training_state = TrainingState.IDLE
            for state in self._all_states:
                state._training_state = TrainingState.IDLE
                if state._fsdp_param_group:
                    state._fsdp_param_group.finalize_backward()
            self._root_post_backward_final_callback_queued = False
            for handle in self._pre_backward_hook_handles:
                handle.remove()
            self._pre_backward_hook_handles.clear()
            self._post_forward_order.clear()

    def _register_pre_backward_hook(self, output: Any) -> Any:
        if not torch.is_grad_enabled():
            return output

        flat_outputs, _ = tree_flatten(output)
        tensors = tuple(t for t in flat_outputs if t.requires_grad)
        if tensors:
            handle = register_multi_grad_hook(tensors, self._pre_backward, mode="any")
            self._pre_backward_hook_handles.append(handle)
            if self._fsdp_param_group:
                self._fsdp_param_group.expected_backward_unshard_count += 1
        return output

    def _register_root_post_backward_final_callback(self):
        if self._root_post_backward_final_callback_queued:
            return
        self._root_post_backward_final_callback_queued = True
        Variable._execution_engine.queue_callback(
            self._root_post_backward_final_callback
        )


def _get_module_fsdp_state(module: nn.Module) -> Optional[FSDPState]:
    state = _get_module_state(module)
    if isinstance(state, FSDPState):
        return state
    return None
