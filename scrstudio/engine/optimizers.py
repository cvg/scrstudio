# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Optimizers class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type

import torch
from torch.cuda.amp.grad_scaler import GradScaler
from torch.nn.parameter import Parameter

from scrstudio.configs import base_config
from scrstudio.utils import writer


# Optimizer related configs
@dataclass
class OptimizerConfig(base_config.PrintableConfig):
    """Basic optimizer config with RAdam"""

    _target: Type = torch.optim.Adam
    """The optimizer class to use."""
    lr: float = 0.0005
    """The learning rate to use."""
    eps: float = 1e-08
    """The epsilon value to use."""
    max_norm: Optional[float] = None
    """The max norm to use for gradient clipping."""

    # TODO: somehow make this more generic. i dont like the idea of overriding the setup function
    # but also not sure how to go about passing things into predefined torch objects.
    def setup(self, params) -> torch.optim.Optimizer:
        """Returns the instantiated object using the config."""
        kwargs = vars(self).copy()
        kwargs.pop("_target")
        kwargs.pop("max_norm")
        return self._target(params, **kwargs)


@dataclass
class AdamOptimizerConfig(OptimizerConfig):
    """Basic optimizer config with Adam"""

    _target: Type = torch.optim.Adam
    weight_decay: float = 0
    """The weight decay to use."""

@dataclass
class AdamWOptimizerConfig(OptimizerConfig):
    """Basic optimizer config with Adam"""

    _target: Type = torch.optim.AdamW
    weight_decay: float = 1e-2
    """The weight decay to use."""


@dataclass
class RAdamOptimizerConfig(OptimizerConfig):
    """Basic optimizer config with RAdam"""

    _target: Type = torch.optim.RAdam
    weight_decay: float = 0
    """The weight decay to use."""


class Optimizers:
    """A set of optimizers.

    Args:
        config: The optimizer configuration object.
        param_groups: A dictionary of parameter groups to optimize.
    """

    def __init__(self, config: Dict[str, Any], param_groups: Dict[str, List[Parameter]]) -> None:
        self.config = config
        self.optimizers = {}
        self.schedulers = {}
        self.parameters = {}
        for param_group_name, params in param_groups.items():
            # For deprecation, catch the camera_opt param group and fix it nicely
            if param_group_name == "camera_opt" and "camera_opt" not in config:
                from scrstudio.engine.schedulers import ExponentialDecaySchedulerConfig
                from scrstudio.utils.rich_utils import CONSOLE

                CONSOLE.print(
                    "\nThe 'camera_opt' param group should be assigned an optimizer in the config. Assigning default optimizers for now. This will be removed in a future release.\n",
                    style="bold yellow",
                )

                config["camera_opt"] = {
                    "optimizer": AdamOptimizerConfig(lr=1e-3, eps=1e-15),
                    "scheduler": ExponentialDecaySchedulerConfig(lr_final=1e-4, max_steps=30000),
                }
            # Print some nice warning messages if the user forgot to specify an optimizer
            if param_group_name not in config:
                raise RuntimeError(
                    f"""Optimizer config for '{param_group_name}' not found in config file. Make sure you specify an optimizer for each parameter group. Provided configs were: {config.keys()}"""
                )
            lr_init = config[param_group_name]["optimizer"].lr
            self.optimizers[param_group_name] = config[param_group_name]["optimizer"].setup(params=params)
            self.parameters[param_group_name] = params
            if config[param_group_name]["scheduler"]:
                self.schedulers[param_group_name] = (
                    config[param_group_name]["scheduler"]
                    .setup()
                    .get_scheduler(optimizer=self.optimizers[param_group_name], lr_init=lr_init)
                )

    def optimizer_step(self, param_group_name: str) -> None:
        """Fetch and step corresponding optimizer.

        Args:
            param_group_name: name of optimizer to step forward
        """
        self.optimizers[param_group_name].step()

    def scheduler_step(self, param_group_name: str) -> None:
        """Fetch and step corresponding scheduler.

        Args:
            param_group_name: name of scheduler to step forward
        """
        if "scheduler" in self.config[param_group_name]:
            self.schedulers[param_group_name].step()

    def zero_grad_all(self) -> None:
        """Zero the gradients for all optimizer parameters."""
        for _, optimizer in self.optimizers.items():
            optimizer.zero_grad()

    def zero_grad_some(self, param_groups: List[str]) -> None:
        """Zero the gradients for the given parameter groups."""
        for param_group in param_groups:
            optimizer = self.optimizers[param_group]
            optimizer.zero_grad()

    def optimizer_scaler_step_all(self, grad_scaler: GradScaler) -> None:
        """Take an optimizer step using a grad scaler.

        Args:
            grad_scaler: GradScaler to use
        """
        for param_group, optimizer in self.optimizers.items():
            max_norm = self.config[param_group]["optimizer"].max_norm
            if max_norm is not None:
                grad_scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(self.parameters[param_group], max_norm)
            if any(any(p.grad is not None for p in g["params"]) for g in optimizer.param_groups):
                grad_scaler.step(optimizer)

    def optimizer_scaler_step_some(self, grad_scaler: GradScaler, param_groups: List[str]) -> None:
        """Take an optimizer step using a grad scaler ONLY on the specified param groups.

        Args:
            grad_scaler: GradScaler to use
        """
        for param_group in param_groups:
            optimizer = self.optimizers[param_group]
            max_norm = self.config[param_group]["optimizer"].max_norm
            if max_norm is not None:
                grad_scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(self.parameters[param_group], max_norm)
            if any(any(p.grad is not None for p in g["params"]) for g in optimizer.param_groups):
                grad_scaler.step(optimizer)

    def optimizer_step_all(self) -> None:
        """Run step for all optimizers."""
        for param_group, optimizer in self.optimizers.items():
            # note that they key is the parameter name
            max_norm = self.config[param_group]["optimizer"].max_norm
            if max_norm is not None:
                torch.nn.utils.clip_grad_norm_(self.parameters[param_group], max_norm)
            optimizer.step()

    def scheduler_step_all(self, step: int) -> None:
        """Run step for all schedulers.

        Args:
            step: the current step
        """
        for param_group_name, scheduler in self.schedulers.items():
            scheduler.step()
            # TODO(ethan): clean this up. why is there indexing into a list?
            lr = scheduler.get_last_lr()[0]
            writer.put_scalar(name=f"learning_rate/{param_group_name}", scalar=lr, step=step)

    def load_optimizers(self, loaded_state: Dict[str, Any]) -> None:
        """Helper to load the optimizer state from previous checkpoint

        Args:
            loaded_state: the state from the previous checkpoint
        """
        for k, v in loaded_state.items():
            self.optimizers[k].load_state_dict(v)

    def load_schedulers(self, loaded_state: Dict[str, Any]) -> None:
        """Helper to load the scheduler state from previous checkpoint

        Args:
            loaded_state: the state from the previous checkpoint
        """
        for k, v in loaded_state.items():
            self.schedulers[k].load_state_dict(v)
