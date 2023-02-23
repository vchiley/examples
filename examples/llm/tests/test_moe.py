# Copyright 2022 MosaicML Examples authors
# SPDX-License-Identifier: Apache-2.0

import os
import warnings
import pytest
import torch

from omegaconf import OmegaConf as om
from composer.utils import dist, reproducibility
from composer.core import Evaluator
from composer import Trainer

from examples.common.builders import (build_algorithm, build_callback,
                                      build_dataloader,
                                      build_logger, build_optimizer,
                                      build_scheduler)
from examples.common.config_utils import log_config
from examples.llm.src.model_registry import COMPOSER_MODEL_REGISTRY


def gpt_tiny_moe_cfg(conf_path='yamls/mosaic_gpt/125m_moe.yaml'):
    """Create gpt tiny moe cfg."""
    with open(conf_path) as f:
        test_cfg = om.load(f)
    test_cfg.train_loader.dataset.split = 'train_small'

    test_cfg.model.init_device = 'cpu'

    bsize = 2
    test_cfg.device_eval_batch_size = bsize
    test_cfg.device_train_microbatch_size = bsize
    test_cfg.global_train_batch_size = 2 * bsize * dist.get_world_size()

    test_cfg.max_duration = '4ba'
    test_cfg.eval_interval = '4ba'
    test_cfg.eval_subset_num_batches = 2
    test_cfg.save_interval = '4ba'
    test_cfg.run_name = 'gpt-moe-test'
    test_cfg.max_seq_len = 256

    test_cfg.tokenizer.args.max_seq_len = test_cfg.max_seq_len
    test_cfg.train_loader.dataset.max_seq_len = test_cfg.max_seq_len
    test_cfg.eval_loader.dataset.max_seq_len = test_cfg.max_seq_len

    test_cfg.model.max_seq_len = test_cfg.max_seq_len
    test_cfg.model.d_model = 32
    test_cfg.model.n_heads = 2
    test_cfg.model.n_layers = 2
    test_cfg.model.moe.num_experts = [2, 16]

    return test_cfg


def check_tensor(tensor):
    world_size = dist.get_world_size()
    tensors = dist.all_gather(tensor)
    for i in range(world_size):
        for j in range(i + 1, world_size):
            assert not tensors[i].equal(tensors[j])
            if tensors[i].all_close(tensors[j]):
                warnings.warn(f'')


def test_tutel_moe_expert_notsync():
    if not os.path.isdir('./my-copy-c4/val') or not os.path.isdir('./my-copy-c4/train_small'):
        pytest.xfail('c4 dataset not set up as expected')
    if not torch.cuda.is_available() and not dist.get_world_size() > 1:
        pytest.xfail('test requires multiple GPUs')

    cfg = gpt_tiny_moe_cfg(conf_path='yamls/mosaic_gpt/125m_moe.yaml')

    reproducibility.seed_all(cfg.seed)

    # Read FSDP Config as a dict
    fsdp_config = cfg.get('fsdp_config', None)
    fsdp_config = om.to_container(fsdp_config,
                                  resolve=True) if fsdp_config else None

    # Restrict model init_device to 'meta' and 'cpu',
    # using 'cuda' vs. 'cuda:id' is tricky and can lead to common user errors
    # when multiple GPUs are available.
    # Also 'meta' is only valid when using FSDP
    init_device = cfg.model.get('init_device', 'cpu')
    assert init_device in ['meta', 'cpu']
    if fsdp_config is None and init_device == 'meta':
        warnings.warn(
            "Using `cfg.model.init_device='meta'` is only valid when using FSDP! "
            "Reverting to `cfg.model.init_device='cpu'`.")
        cfg.model.init_device = 'cpu'

    # Build Model
    print('Initializing model...')
    model = COMPOSER_MODEL_REGISTRY[cfg.model.name](cfg.model)
    if hasattr(model, 'param_count'):
        cfg.n_params = model.param_count
    else:
        cfg.n_params = sum(p.numel() for p in model.parameters())
    print(f'{cfg.n_params=:.2e}')
    if hasattr(model, 'num_fwd_flops'):
        print(f'{model.num_fwd_flops=:.2e}')

    model = model.cuda()

    for n, m in model.model.named_modules():
        if n.endswith(".mlp.moe.experts"):
            for _n, p in m.named_parameters():
                # verify expert parameters are initialized independently
                check_tensor(p)
                assert p.grad is None

    # Build the Trainer
    print('Building trainer...')
    trainer = Trainer(
        run_name=cfg.run_name,
        seed=cfg.seed,
        model=model,
        train_dataloader=build_dataloader(cfg.train_loader,
                                          cfg.device_train_microbatch_size),
        eval_dataloader=[Evaluator(
            label='eval',
            dataloader=build_dataloader(cfg.eval_loader, cfg.device_eval_batch_size),
            metric_names=list(model.train_metrics.keys())
        )],
        optimizers=build_optimizer(cfg.optimizer, model),
        schedulers=build_scheduler(cfg.scheduler),
        max_duration=cfg.max_duration,
        eval_interval=cfg.eval_interval,
        eval_subset_num_batches=cfg.get('eval_subset_num_batches', -1),
        progress_bar=cfg.get('progress_bar', False),
        log_to_console=cfg.get('log_to_console', True),
        console_log_interval=cfg.get('console_log_interval', '1ba'),
        loggers=[
            build_logger(name, logger_cfg)
            for name, logger_cfg in (cfg.get('loggers') or {}).items()
        ],
        callbacks=[
            build_callback(name, callback_cfg)
            for name, callback_cfg in (cfg.get('callbacks') or {}).items()
        ],
        precision=cfg.precision,
        algorithms=[
            build_algorithm(name, algorithm_cfg)
            for name, algorithm_cfg in (cfg.get('algorithms') or {}).items()
        ],
        device_train_microbatch_size=cfg.get('device_train_microbatch_size',
                                             'auto'),
        fsdp_config=fsdp_config,  # type: ignore
        save_folder=cfg.get('save_folder', None),
        save_interval=cfg.get('save_interval', '1000ba'),
        save_num_checkpoints_to_keep=cfg.get('save_num_checkpoints_to_keep',
                                             -1),
        save_overwrite=cfg.get('save_overwrite', False),
        load_path=cfg.get('load_path', None),
        load_weights_only=cfg.get('load_weights_only', False),
    )

    print('Logging config...')
    log_config(cfg)

    for n, m in trainer.state.model.model.named_modules():
        if n.endswith(".mlp.moe.experts"):
            for _n, p in m.named_parameters():
                # verify expert parameters are initialized independently
                check_tensor(p)
                assert p.grad is None

    print('Starting training...')
    trainer.fit()

    for n, m in trainer.state.model.model.named_modules():
        if n.endswith(".mlp.moe.experts"):
            for _n, p in m.named_parameters():
                # verify expert parameters not expert parameter gradients are sync'd
                check_tensor(p)
                check_tensor(p.grad)

    print('Done.')


if __name__ == "__main__":
    test_tutel_moe_expert_notsync()