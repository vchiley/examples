# Copyright 2022 MosaicML Examples authors
# SPDX-License-Identifier: Apache-2.0

"""A simple, flexible implementation of a GPT model.

Inspired by https://github.com/karpathy/minGPT/blob/master/mingpt/model.py
"""

import math
import warnings
from functools import partial
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from composer.metrics.nlp import LanguageCrossEntropy, Perplexity
from composer.models.base import ComposerModel
from composer.utils import get_device, dist
from omegaconf import DictConfig
from omegaconf.listconfig import ListConfig

from .mosaic_moe.moe_layer import MOELayer
from .mosaic_moe.gates.top import TopKGate, LinearTopKGate
from .mosaic_moe.experts.ffn import FusedExpertsNetwork

from .mosaic_moe.experts.utils import SeedContextManager


class TorchCausalAttention(nn.Module):

    def __init__(self, cfg: DictConfig, device: Optional[str] = None):
        super().__init__()
        self.mhsa = nn.MultiheadAttention(
            embed_dim=cfg.d_model,
            num_heads=cfg.n_heads,
            dropout=cfg.attn_pdrop,
            bias=True,
            batch_first=True,
            device=device,
        )
        self.mhsa.out_proj._is_residual = True  # type: ignore

        warnings.warn(
            DeprecationWarning(
                'Using `attn_impl: torch` is deprecated; recommened using `attn_impl: triton`.'
            ))

    def forward(self, x, key_padding_mask, attn_mask=None):
        return self.mhsa(x,
                         x,
                         x,
                         attn_mask=attn_mask,
                         key_padding_mask=~key_padding_mask,
                         need_weights=True)

    @staticmethod
    def mask_shape(n_heads, seq_len, alibi):
        if alibi:
            return (n_heads, seq_len, seq_len)
        return (seq_len, seq_len)

    @staticmethod
    def attn_mask_(attn_mask, n_heads, seq_len, alibi=False, alibi_bias_max=8):
        # in-place fill causal attn mask
        #
        # Two important disclaimers
        # 1. Torch uses additive attention. If your attn_mask/key_padding mask is a float tensor, it will add the floats
        #   directly to your attention matrix. If they are boolean masks, True will be converted to -inf before adding the
        #   mask to your attentions. See https://pytorch.org/docs/stable/generated/torch.nn.MultiheadAttention.html#torch.nn.MultiheadAttention.forward
        #   Basically True/-inf indicates tokens we do not want to attend to.
        #
        # 2. This is is the exact opposite behavior of Huggingface's tokenizers, which use the convention that True denotes tokens
        #   we do want to attend to. See https://huggingface.co/docs/transformers/glossary#attention-mask
        attn_mask.fill_(float('-inf'))
        attn_mask.triu_(diagonal=1)

        if alibi:
            device, dtype = attn_mask.device, attn_mask.dtype
            a_bias = alibi_bias(n_heads,
                                seq_len,
                                full=True,
                                alibi_bias_max=alibi_bias_max,
                                device=device,
                                dtype=dtype)
            attn_mask.add_(a_bias.squeeze())

        return attn_mask


class FlashCausalAttention(nn.Module):

    def __init__(self, cfg: DictConfig, device: Optional[str] = None):
        super().__init__()
        try:
            from flash_attn.flash_attention import FlashMHA  # type: ignore
        except ImportError as e:
            raise e

        self.mhsa = FlashMHA(
            embed_dim=cfg.d_model,
            num_heads=cfg.n_heads,
            attention_dropout=cfg.attn_pdrop,
            bias=True,
            batch_first=True,
            causal=True,
            device=device,
        )
        self.mhsa.out_proj._is_residual = True

        warnings.warn(
            DeprecationWarning(
                'Using `attn_impl: flash` is deprecated; recommened using `attn_impl: triton`.'
            ))

    def forward(self, x, key_padding_mask, attn_mask=None):
        assert attn_mask is None
        return self.mhsa(x,
                         key_padding_mask=key_padding_mask,
                         need_weights=False)

    @staticmethod
    def mask_shape(*args, **kwargs):
        return None

    @staticmethod
    def attn_mask_(*args, **kwargs):
        return None


class TritonFlashCausalAttention(nn.Module):
    """Multi-headed self attention using triton FlashAttn kernel.

    This also includes bias for Alibi integration.
    """

    def __init__(self, cfg: DictConfig, device: Optional[str] = None):
        super().__init__()
        try:
            from examples.llm.src.flash_attention import \
                FlashMHA  # type: ignore
        except ImportError as e:
            raise e

        assert cfg.attn_pdrop == 0, 'triton kernel does not support attn_dropout'

        self.mhsa = FlashMHA(
            embed_dim=cfg.d_model,
            num_heads=cfg.n_heads,
            bias=True,
            batch_first=True,
            causal=True,
            device=device,
        )
        self.mhsa.out_proj._is_residual = True  # type: ignore

    def forward(self, x, key_padding_mask=None, attn_mask=None):
        assert key_padding_mask is None
        return self.mhsa(x,
                         key_padding_mask=None,
                         attn_mask=attn_mask,
                         need_weights=False)

    @staticmethod
    def mask_shape(n_heads, seq_len, alibi):
        return (1, n_heads, 1, seq_len) if alibi else (1, 1, 1, seq_len)

    @staticmethod
    def attn_mask_(attn_mask, n_heads, seq_len, alibi=False, alibi_bias_max=8):
        # in-place fill causal attn mask
        attn_mask.zero_()

        if alibi:
            device, dtype = attn_mask.device, attn_mask.dtype
            attn_mask.add_(
                alibi_bias(n_heads,
                           seq_len,
                           full=False,
                           alibi_bias_max=alibi_bias_max,
                           device=device,
                           dtype=dtype))

        return attn_mask


def alibi_bias(n_heads,
               seq_len,
               full=False,
               alibi_bias_max=8,
               device=None,
               dtype=None):
    alibi_bias = torch.arange(1 - seq_len, 1, dtype=dtype,
                              device=device).view(1, 1, 1, seq_len)
    if full:
        # generate 1 x Heads x SeqLen x SeqLen alibi bias mask
        # otherwise the mask is 1 x Heads x 1 x SeqLen (which is braodcasted up to the approproate size)
        alibi_bias = alibi_bias - torch.arange(
            1 - seq_len, 1, dtype=dtype, device=device).view(1, 1, seq_len, 1)
        alibi_bias.abs_().mul_(
            -1
        )  # since we're using causal flag, this isn't really needed, but why not include it

    m = torch.arange(1, n_heads + 1, dtype=dtype, device=device)
    m.mul_(alibi_bias_max / n_heads)
    alibi_bias = alibi_bias * (1. / (2**m.view(1, n_heads, 1, 1)))
    return alibi_bias


class GPTMLP(nn.Module):

    def __init__(self, cfg: DictConfig, device: Optional[str] = None):
        super().__init__()
        self.mlp_up = nn.Linear(cfg.d_model,
                                cfg.mlp_ratio * cfg.d_model,
                                device=device)
        self.mlp_act = nn.GELU(approximate='none')
        self.mlp_down = nn.Linear(cfg.mlp_ratio * cfg.d_model,
                                  cfg.d_model,
                                  device=device)
        self.mlp_down._is_residual = True  # type: ignore

    def forward(self, x):
        return self.mlp_down(self.mlp_act(self.mlp_up(x)))


class GPTMLPMoE(nn.Module):

    def __init__(self, cfg: DictConfig, block_idx: int, device: Optional[str] = None):
        super().__init__()
        dist.initialize_dist(get_device(None), timeout=1800)
        num_experts = cfg.moe.get('num_experts')
        if isinstance(num_experts, ListConfig):
            # enables pyramid moe
            num_experts = num_experts[block_idx]

        rank_seed = torch.initial_seed() + dist.get_global_rank()
        with SeedContextManager(gpu_devices=[torch.cuda.current_device()], seed=rank_seed) as s_ctx_mgr:
            expert = FusedExpertsNetwork(
                in_features=cfg.d_model,
                hidden_features=cfg.mlp_ratio * cfg.d_model,
                num_global_experts=num_experts,
                out_features=cfg.d_model,
                bias=True,
                activation_fn=lambda x: F.gelu(x, approximate='none'),
                a2a_ffn_overlap_degree=cfg.moe.get('a2a_ffn_overlap_degree', 1),
                is_postscore=cfg.moe.get('is_postscore', True),
                parallel_type=cfg.moe.get('parallel_type', 'auto'),
                use_2dh=cfg.moe.get('use_2dh', False),
                group=cfg.moe.get('group', None),
                scan_expert_func=lambda name, param: setattr(param, '_moe_expert', True),
                device=device,
                dtype=None,
            )
            expert.batched_fc2_weight._is_residual = True  # type: ignore

        gate = LinearTopKGate(
            in_features=cfg.d_model,
            num_global_experts=num_experts,
            k=cfg.moe.get('gate_k', 1),
            fp32_gate=cfg.moe.get('fp32_gate', True),
            capacity_factor=cfg.moe.get('capacity_factor', 1.),
            gate_noise=cfg.moe.get('gate_noise', 0.),
            inequivalent_tokens=cfg.moe.get('inequivalent_tokens', False),
            batch_prioritized_routing=cfg.moe.get('batch_prioritized_routing', False),
            normalize_gate=cfg.moe.get('normalize_gate', True),
            is_gshard_loss=cfg.moe.get('is_gshard_loss', True),
            group=expert.group,
            device=device,
            dtype=None,
        )

        self.moe = MOELayer(gate=gate, expert=expert)

    def forward(self, x):
        return self.moe(x)


class GPTBlock(nn.Module):

    def __init__(self,
                 cfg: DictConfig,
                 causal_attn_cls,
                 block_idx: int,
                 device: Optional[str] = None):
        super().__init__()
        if cfg.get('alibi', False):
            assert cfg.attn_impl == 'triton' or cfg.attn_impl == 'torch', 'Only triton kernel or torch supports alibi'
        self.ln_1 = nn.LayerNorm(cfg.d_model, device=device)
        self.causal_attn = causal_attn_cls(cfg, device)
        self.ln_2 = nn.LayerNorm(cfg.d_model, device=device)

        use_moe = False
        if cfg.get('moe', None):
            num_experts = cfg.moe.get('num_experts')
            if isinstance(num_experts, ListConfig):
                num_experts = num_experts[block_idx]
            use_moe = True if num_experts > 1 else False

        self.mlp = GPTMLPMoE(cfg, block_idx, device=device) if use_moe else GPTMLP(cfg, device=device)
        self.resid_attn_dropout = nn.Dropout(cfg.resid_pdrop)
        self.resid_mlp_dropout = nn.Dropout(cfg.resid_pdrop)

    def forward(
        self,
        x: torch.Tensor,
        key_padding_mask: Optional[torch.ByteTensor] = None,
        attn_mask: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        a = self.ln_1(x)
        b, _ = self.causal_attn(a, key_padding_mask, attn_mask)
        x = x + self.resid_attn_dropout(b)
        m = self.ln_2(x)
        n = self.mlp(m)
        x = x + self.resid_mlp_dropout(n)
        return x


class MosaicGPT(nn.Module):

    def __init__(self, cfg: DictConfig):
        super().__init__()
        assert cfg.name == 'mosaic_gpt', f'Tried to build MosaicGPT model with cfg.name={cfg.name}'
        self.cfg = cfg
        if cfg.attn_impl == 'torch':
            self.causal_attn_cls = TorchCausalAttention
        elif cfg.attn_impl == 'flash':
            self.causal_attn_cls = FlashCausalAttention
        elif cfg.attn_impl == 'triton':
            self.causal_attn_cls = TritonFlashCausalAttention
        else:
            raise ValueError(f'Unknown attn_impl={cfg.attn_impl}')

        self.alibi = cfg.get('alibi', False)
        self.alibi_bias_max = cfg.get('alibi_bias_max',
                                      8 if self.alibi else None)
        # CogView (https://arxiv.org/abs/2105.13290) and GLM-130B (https://arxiv.org/abs/2210.02414)
        # both report this helping with stabilizing training
        self.embedding_fraction = cfg.get('embedding_fraction', 1)
        assert 0 < self.embedding_fraction <= 1, 'model.embedding_fraction must be between 0 (exclusive) and 1 (inclusive)!'

        self.transformer = nn.ModuleDict({
            'wte': nn.Embedding(cfg.vocab_size, cfg.d_model, device=cfg.device)
        })
        if not self.alibi:
            self.transformer.update({
                'wpe':
                    nn.Embedding(cfg.max_seq_len,
                                 cfg.d_model,
                                 device=cfg.device)
            })
        self.transformer.update({'emb_drop': nn.Dropout(cfg.emb_pdrop)})
        self.transformer.update({
            'blocks':
                nn.ModuleList([
                    GPTBlock(cfg,
                             causal_attn_cls=self.causal_attn_cls,
                             block_idx=idx,
                             device=cfg.device) for idx in range(cfg.n_layers)
                ])
        })
        self.transformer.update(
            {'ln_f': nn.LayerNorm(cfg.d_model, device=cfg.device)})

        if cfg.device != 'meta':
            self.apply(self.param_init_fn)

        # define attn mask
        self._attn_mask_initialized = False
        mask_shape = self.causal_attn_cls.mask_shape(cfg.n_heads,
                                                     cfg.max_seq_len,
                                                     self.alibi)
        if mask_shape:
            self.register_buffer('attn_mask',
                                 torch.empty(mask_shape, device=cfg.device))
        else:
            self.attn_mask = None

    def _attn_mask(self, batch_size=None, seq_len=None, key_padding_mask=None):
        if not self._attn_mask_initialized:
            self.causal_attn_cls.attn_mask_(self.attn_mask,
                                            self.cfg.n_heads,
                                            self.cfg.max_seq_len,
                                            alibi=self.alibi,
                                            alibi_bias_max=self.alibi_bias_max)
            self._attn_mask_initialized = True

        if self.cfg.attn_impl == 'flash':
            return self.attn_mask  # None

        # select seq_len subset of attn mask
        assert self.attn_mask is not None, 'Internal logic error'
        attn_mask = self.attn_mask[..., :seq_len, :seq_len]

        if self.cfg.attn_impl == 'triton' and key_padding_mask is not None and key_padding_mask.bool(
        ).logical_not().any():
            attn_mask = attn_mask.masked_fill(
                ~key_padding_mask.view(batch_size, 1, 1, seq_len),
                float('-inf'))

        if self.cfg.attn_impl == 'torch':
            if key_padding_mask is not None and key_padding_mask.bool(
            ).logical_not().any():
                attn_mask = attn_mask.expand(batch_size, self.cfg.n_heads,
                                             seq_len, seq_len).clone()
                attn_mask.masked_fill_(
                    ~key_padding_mask.view(batch_size, 1, 1, seq_len),
                    float('-inf'))
                attn_mask = attn_mask.reshape(-1, seq_len, seq_len)
            elif self.alibi:
                # WARNING: Alibi with torch attn is not thoroughly tested
                # torch mask is supposed to be of shape nzz x SeqLen x SeqLen
                # we must braodcast to batch size then flatten batchsize * n_heads dim
                # Note: if key_padding_mask is triggered, the needed expansion is already done.
                attn_mask = attn_mask.expand(batch_size, self.cfg.n_heads,
                                             seq_len, seq_len).reshape(
                                                 -1, seq_len, seq_len)

        return attn_mask

    def forward(self,
                input_ids: torch.LongTensor,
                key_padding_mask: Optional[torch.ByteTensor] = None):
        B, S = input_ids.size()
        assert (
            S <= self.cfg.max_seq_len
        ), f'Cannot forward input with seq_len={S}, this model only supports seq_len<={self.cfg.max_seq_len}'

        tok_emb = self.transformer.wte(input_ids)  # type: ignore
        if self.alibi:
            x = tok_emb
        else:
            pos = torch.arange(0, S, dtype=torch.long,
                               device=input_ids.device).unsqueeze(0)
            pos_emb = self.transformer.wpe(pos)  # type: ignore
            x = tok_emb + pos_emb

        if self.embedding_fraction == 1:
            x = self.transformer.emb_drop(x)  # type: ignore
        else:
            # this implementation is proposed on page 7 of the GLM-130B paper https://arxiv.org/abs/2210.02414
            x_shrunk = (x * self.embedding_fraction) + (
                x.detach() * (1 - self.embedding_fraction))
            assert isinstance(self.transformer.emb_drop, nn.Module)  # pyright
            x = self.transformer.emb_drop(x_shrunk)

        attn_mask = self._attn_mask(batch_size=B,
                                    seq_len=S,
                                    key_padding_mask=key_padding_mask)
        for block in self.transformer.blocks:  # type: ignore
            x = block(
                x, None if self.cfg.attn_impl == 'triton' else key_padding_mask,
                attn_mask)
        x = self.transformer.ln_f(x)  # type: ignore
        # output embedding weight tied to input embedding
        assert isinstance(self.transformer.wte, nn.Module)  # pyright
        assert isinstance(self.transformer.wte.weight, torch.Tensor)  # pyright
        logits = F.linear(x, self.transformer.wte.weight, None)
        return logits

    # Param Initialization, needed for device='meta' fast initialization
    def param_init_fn(self, module):
        init_fn = partial(torch.nn.init.normal_,
                          mean=0.0,
                          std=self.cfg.init_std)
        # Linear
        if isinstance(module, nn.Linear):
            init_fn(module.weight)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)

            if getattr(module, '_is_residual', False):
                module.weight.data.normal_(
                    mean=0.0,
                    std=(self.cfg.init_std / math.sqrt(2 * self.cfg.n_layers)))

        # Embedding
        if isinstance(module, nn.Embedding):
            init_fn(module.weight)

        # LayerNorm
        if isinstance(module, nn.LayerNorm):
            torch.nn.init.zeros_(module.bias)
            torch.nn.init.ones_(module.weight)

        # torch's MultiheadAttention
        if isinstance(module, nn.MultiheadAttention):
            if module._qkv_same_embed_dim:
                assert module.in_proj_weight is not None
                assert module.q_proj_weight is None and module.k_proj_weight is None and module.v_proj_weight is None
                init_fn(module.in_proj_weight)
            else:
                assert module.q_proj_weight is not None and module.k_proj_weight is not None and module.v_proj_weight is not None
                assert module.in_proj_weight is None
                init_fn(module.q_proj_weight)
                init_fn(module.k_proj_weight)
                init_fn(module.v_proj_weight)

            # bias
            if module.in_proj_bias is not None:
                torch.nn.init.zeros_(module.in_proj_bias)
            if module.bias_k is not None:
                torch.nn.init.zeros_(module.bias_k)
            if module.bias_v is not None:
                torch.nn.init.zeros_(module.bias_v)

            # out proj
            if module.out_proj._is_residual:
                module.out_proj.weight.data.normal_(
                    mean=0.0,
                    std=(self.cfg.init_std / math.sqrt(2 * self.cfg.n_layers)))
            else:
                init_fn(module.out_proj.weight)
            if module.out_proj.bias is not None:
                torch.nn.init.zeros_(module.out_proj.bias)

        if isinstance(module, FusedExpertsNetwork):
            # although the module is supposed to be ignored by FSDP,
            # model.apply will still run param_init_fn on the entire model
            # so FusedExpertsNetwork must be initialized here

            # guarentee init seeds are different for all devices
            # SeedContextManager sets the given seed, then restore local when finished
            rank_seed = torch.initial_seed() + dist.get_global_rank()
            with SeedContextManager(module.batched_fc1_weight, module.batched_fc2_weight, seed=rank_seed) as s_ctx_mgr:
                if self.cfg.get('init_experts', False):
                    init_fn(module.batched_fc1_weight)
                    module.batched_fc2_weight.data.normal_(
                        mean=0.0,
                        std=(self.cfg.init_std / math.sqrt(2 * self.cfg.n_layers)))
                else:
                    # use default linear init since that is what is used by tutel
                    # still zero init bias)
                    torch.nn.init.kaiming_uniform_(module.batched_fc1_weight, a=math.sqrt(5))
                    torch.nn.init.kaiming_uniform_(module.batched_fc2_weight, a=math.sqrt(5))
            torch.nn.init.zeros_(module.batched_fc1_bias)
            torch.nn.init.zeros_(module.batched_fc2_bias)

        if isinstance(module, TopKGate):
            # Linear handeled by nn.Linear
            pass

    # custom FSDP wrapping to enable MoE expert sharding
    @staticmethod
    def fsdp_custom_wrap(model, fsdp_kwargs):
        from torch.distributed.fsdp import FullyShardedDataParallel

        # first wrap experts
        for n, m in model.named_modules():
            if hasattr(m, "expert"):
                # WARNING: tutel handles its own sharding so we just set the current rank.
                # MoE implementations which require an outside library to handle expert sharding,
                # we need to explicitly pass all ranks participating in the MoE expert shard.
                ranks = [dist.get_global_rank()]
                m.expert = FullyShardedDataParallel(
                    m.expert,
                    process_group=torch.distributed.new_group(ranks=ranks), # TODO (other args: timeout=default_pg_timeout, backend=None, pg_options=None)
                    **fsdp_kwargs
                )

        # wrap GPTBlock
        for n, m in model.named_modules():
            if hasattr(m, "blocks"):
                for i in range(len(m.blocks)):
                    m.blocks[i] = FullyShardedDataParallel(m.blocks[i], **fsdp_kwargs)

    # Activation Checkpointing
    def activation_checkpointing_fn(self, module):
        return isinstance(module, GPTBlock)


class ComposerMosaicGPT(ComposerModel):

    def __init__(self, cfg):
        super().__init__()
        self.model = MosaicGPT(cfg)
        self.__param_count = None
        self.__num_fwd_flops = None
        self.train_metrics = {
            'LanguageCrossEntropy': LanguageCrossEntropy(cfg.vocab_size),
            'Perplexity': Perplexity(),
        }
        self.eval_metrics = {
            'LanguageCrossEntropy': LanguageCrossEntropy(cfg.vocab_size),
            'Perplexity': Perplexity(),
        }

    def get_targets(self, batch):
        targets = torch.roll(batch['labels'], shifts=-1)
        targets[:, -1] = -100
        return targets

    def forward(self, batch):
        return self.model(batch['input_ids'],
                          key_padding_mask=batch['attention_mask'].bool())

    def eval_forward(self, batch, outputs=None):
        return outputs if outputs is not None else self.forward(batch)

    def loss(self, outputs, batch):
        targets = self.get_targets(batch)
        loss = F.cross_entropy(outputs.view(-1, outputs.size(-1)),
                               targets.view(-1),
                               ignore_index=-100)

        if self.model.cfg.get('moe', None) is not None:
            for n, m in self.named_modules():
                # pretty bad way to identify MoE layer, but it is what it is...
                if n[-len('.moe'):] == '.moe':
                    loss += self.model.cfg.moe.l_aux_wt * m.l_aux

        return loss

    def get_metrics(self, is_train=False):
        return self.train_metrics if is_train else self.eval_metrics

    def update_metric(self, batch, outputs, metric):
        outputs = outputs.view(-1, outputs.size(-1))
        targets = self.get_targets(batch).view(-1)
        metric.update(outputs, targets)

    @property
    def param_count(self):
        if self.__param_count:
            return self.__param_count

        if self.model.cfg.get('moe', None) is not None:
            n_params_experts = 0
            for n, m in self.named_modules():
                # pretty bad way to identify MoE layer, but it is what it is...
                if n[-len('.moe'):] == '.moe':
                    num_local_experts = m.expert.num_local_experts
                    num_shards = m.expert.num_shards
                    num_global_experts = m.expert.num_global_experts

                    _n_params_expert = sum(p.numel() for _n, p in m.named_parameters() if 'expert' in _n)
                    n_params_experts += _n_params_expert // num_local_experts * num_shards * num_global_experts

            n_params_other = sum(p.numel() for n, p in self.named_parameters() if 'expert' not in n)
            print(f'{n_params_other=}; {n_params_experts=}')
            self.__param_count = n_params_other + n_params_experts
        else:
            self.__param_count = sum(p.numel() for p in self.parameters())

        return self.__param_count

    @property
    def num_fwd_flops(self):
        if self.__num_fwd_flops:
            return self.__num_fwd_flops

        if self.model.cfg.get('moe', None) is not None:
            n_params_expert_active = 0
            for n, m in self.named_modules():
                # pretty bad way to identify MoE layer, but it is what it is...
                if n[-len('.moe'):] == '.moe':
                    gate_k = m.gate.top_k
                    capacity_factor = m.gate.capacity_factor
                    num_local_experts = m.expert.num_local_experts
                    num_shards = m.expert.num_shards

                    _n_params_expert_active = sum(p.numel() for _n, p in m.named_parameters() if 'expert' in _n)
                    _n_params_expert_active = gate_k * _n_params_expert_active // num_local_experts * num_shards
                    # artificially inflate to account for additional flops due to capacity factor
                    n_params_expert_active += capacity_factor * _n_params_expert_active

            n_params_other = sum(p.numel() for n, p in self.named_parameters() if 'expert' not in n)
            n_active_params = n_params_other + n_params_expert_active
        else:
            n_active_params = self.param_count

        # the number of paramters is approximately the number of multiply-accumulates (MAC) in the network
        # each MAC has 2 FLOPs - we multiply by 2 ie 2 * n_param
        # this gets us FLOPs / token
        params_flops_per_token = 2 * n_active_params
        params_flops_per_seq = params_flops_per_token * self.model.cfg.max_seq_len
        # there are 2 FLOPS per mac; there is A=Q*K^T and out=A*V ops (ie mult by 2)
        attn_flops_per_seq = self.model.cfg.n_layers * 2 * 2 * (
            self.model.cfg.d_model * (self.model.cfg.max_seq_len**2))
        self.__num_fwd_flops = params_flops_per_seq + attn_flops_per_seq
        return self.__num_fwd_flops