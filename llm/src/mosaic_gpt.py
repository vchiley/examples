# Copyright 2022 MosaicML Examples authors
# SPDX-License-Identifier: Apache-2.0

"""A simple, flexible implementation of a GPT model.

Inspired by https://github.com/karpathy/minGPT/blob/master/mingpt/model.py
"""

import math
from functools import partial
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from composer.metrics.nlp import LanguageCrossEntropy, Perplexity
from composer.models.base import ComposerModel
from omegaconf import DictConfig


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

    def forward(self, x, key_padding_mask, attn_mask=None):
        return self.mhsa(
            x,
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
            a_bias = alibi_bias(n_heads, seq_len, full=True, alibi_bias_max=alibi_bias_max, device=device, dtype=dtype)
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

    def forward(self, x, key_padding_mask, attn_mask=None):
        assert attn_mask is None
        return self.mhsa(
            x,
            key_padding_mask=key_padding_mask,
            need_weights=False)

    @staticmethod
    def mask_shape(*args, **kwargs):
        return None

    @staticmethod
    def attn_mask_(*args, **kwargs):
        return None


class TritonFlashCausalAttention(nn.Module):
    """Multi-headed self attention using triton FlashAttn kernel which includes bias for Alibi integration
    """

    def __init__(self, cfg: DictConfig, device: Optional[str] = None):
        super().__init__()
        try:
            from src.flash_attention import FlashMHA  # type: ignore
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

    def forward(self, x, key_padding_mask=None, attn_mask=None):
        assert key_padding_mask is None
        return self.mhsa(
            x,
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
            attn_mask.add_(alibi_bias(n_heads, seq_len, full=False, alibi_bias_max=alibi_bias_max, device=device, dtype=dtype))

        return attn_mask


def alibi_bias(n_heads, seq_len, full=False, alibi_bias_max=8, device=None, dtype=None):
    if full:
        # generate 1 x Heads x SeqLen x SeqLen alibi bias mask
        alibi_bias = torch.arange(1 - seq_len, 1, dtype=dtype, device=device).view(1, 1, 1, seq_len)
        alibi_bias = alibi_bias - torch.arange(1 - seq_len, 1, dtype=dtype, device=device).view(1, 1, seq_len, 1)
        alibi_bias.abs_().mul_(-1)
    else:
        # otherwise the mask is 1 x Heads x 1 x SeqLen (which is braodcasted up to the approproate size)
        alibi_bias = torch.arange(0, -seq_len, -1, dtype=dtype, device=device).view(1, 1, 1, seq_len)

    m = torch.arange(1, n_heads + 1, dtype=dtype, device=device)
    m.mul_(alibi_bias_max / n_heads)
    alibi_bias = alibi_bias * (1. / (2 ** m.view(1, n_heads, 1, 1)))
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


class GPTBlock(nn.Module):

    def __init__(self, cfg: DictConfig, causal_attn_cls, device: Optional[str] = None):
        super().__init__()
        if cfg.get('alibi', False):
            assert cfg.attn_impl == 'triton' or cfg.attn_impl == 'torch', 'Only triton kernel or torch supports alibi'
        self.ln_1 = nn.LayerNorm(cfg.d_model, device=device)
        self.causal_attn = causal_attn_cls(cfg, device)
        self.ln_2 = nn.LayerNorm(cfg.d_model, device=device)
        self.mlp = GPTMLP(cfg, device=device)
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

        self.alibi = cfg.get("alibi", False)
        self.alibi_bias_max = cfg.get("alibi_bias_max", 8 if self.alibi else None)
        # CogView (https://arxiv.org/abs/2105.13290) and GLM-130B (https://arxiv.org/abs/2210.02414)
        # both report this helping with stabilizing training
        self.embedding_fraction = cfg.get("embedding_fraction", 1)
        assert 0 < self.embedding_fraction <= 1, "model.embedding_fraction must be between 0 (exclusive) and 1 (inclusive)!"

        self.transformer = nn.ModuleDict({'wte': nn.Embedding(cfg.vocab_size, cfg.d_model, device=cfg.device)})
        if not self.alibi:
            self.transformer.update({'wpe': nn.Embedding(cfg.max_seq_len, cfg.d_model, device=cfg.device)})
        self.transformer.update({'emb_drop': nn.Dropout(cfg.emb_pdrop)})
        self.transformer.update({'blocks': nn.ModuleList([
                    GPTBlock(cfg, causal_attn_cls=self.causal_attn_cls, device=cfg.device)
                    for _ in range(cfg.n_layers)
                ])})
        self.transformer.update({'ln_f': nn.LayerNorm(cfg.d_model, device=cfg.device)})

        if cfg.device != 'meta':
            self.apply(self.param_init_fn)

        # define attn mask
        self._attn_mask_initialized = False
        mask_shape = self.causal_attn_cls.mask_shape(cfg.n_heads, cfg.max_seq_len, self.alibi)
        if mask_shape:
            self.register_buffer('attn_mask', torch.empty(mask_shape, device=cfg.device))
        else:
            self.attn_mask = None

    def _attn_mask(self, x, key_padding_mask=None):

        if not self._attn_mask_initialized:
            self.causal_attn_cls.attn_mask_(
                self.attn_mask,
                self.cfg.n_heads,
                self.cfg.max_seq_len,
                alibi=self.alibi,
                alibi_bias_max=self.alibi_bias_max)
            self._attn_mask_initialized = True

        if self.cfg.attn_impl == 'triton' and key_padding_mask is not None and key_padding_mask.bool().logical_not().any():
            return self.attn_mask.masked_fill(~key_padding_mask.view(-1, 1, 1, self.cfg.max_seq_len), float('-inf'))

        if self.cfg.attn_impl == 'torch' and len(self.attn_mask.shape) == 3:
            # WARNING: Alibi with torch attn is not thoroughly tested
            # torch mask is supposed to be of shape nzz x SeqLen x SeqLen
            # we must braodcast to batch size then flatten batchsize * n_heads dim
            batch_size = x.size(0)
            return self.attn_mask.expand(batch_size, *self.attn_mask.shape).reshape(-1, *self.attn_mask.shape[-2:])
            
        return self.attn_mask

    def forward(self,
                input_ids: torch.LongTensor,
                key_padding_mask: Optional[torch.ByteTensor] = None):
        _, S = input_ids.size()
        assert (
            S <= self.cfg.max_seq_len
        ), f'Cannot forward input with seq_len={S}, this model only supports seq_len<={self.cfg.max_seq_len}'
        pos = torch.arange(0, S, dtype=torch.long, device=input_ids.device).unsqueeze(0)

        tok_emb = self.transformer.wte(input_ids)  # type: ignore

        attn_mask = self._attn_mask(tok_emb, key_padding_mask=key_padding_mask)

        if self.alibi:
            x = tok_emb
        else:
            pos_emb = self.transformer.wpe(pos)  # type: ignore
            x = tok_emb + pos_emb

        if self.embedding_fraction == 1:
            x = self.transformer.emb_drop(x)  # type: ignore
        else:
            # this implementation is proposed on page 7 of the GLM-130B paper https://arxiv.org/abs/2210.02414
            x = self.transformer.emb_drop(
                x * self.embedding_fraction + x.detach() * (1 - self.embedding_fraction)
            )
        for block in self.transformer.blocks:  # type: ignore
            x = block(
                x,
                None if self.cfg.attn_impl == 'triton' else key_padding_mask,
                attn_mask)
        x = self.transformer.ln_f(x)  # type: ignore
        # output embedding weight tied to input embedding
        logits = F.linear(x, self.transformer.wte.weight, None)
        return logits

    # Param Initialization, needed for device='meta' fast initialization
    def param_init_fn(self, module):
        init_fn = partial(torch.nn.init.normal_, mean=0.0, std=self.cfg.init_std)
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

    # FSDP Wrap function
    def fsdp_wrap_fn(self, module):
        return isinstance(module, GPTBlock)

    # Activation Checkpointing
    def activation_checkpointing_fn(self, module):
        return isinstance(module, GPTBlock)


class ComposerMosaicGPT(ComposerModel):

    def __init__(self, cfg):
        super().__init__()
        self.model = MosaicGPT(cfg)
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
        return F.cross_entropy(outputs.view(-1, outputs.size(-1)),
                               targets.view(-1),
                               ignore_index=-100)

    def get_metrics(self, is_train=False):
        return self.train_metrics if is_train else self.eval_metrics

    def update_metric(self, batch, outputs, metric):
        outputs = outputs.view(-1, outputs.size(-1))
        targets = self.get_targets(batch).view(-1)
        metric.update(outputs, targets)

    @property
    def num_fwd_flops(self):
        if self.__num_fwd_flops:
            return self.__num_fwd_flops
        n_params = sum(p.numel() for p in self.parameters())
        # the number of paramters is approximately the number of multiply-accumulates (MAC) in the network
        # each MAC has 2 FLOPs - we multiply by 2 ie 2 * n_param
        # this gets us FLOPs / token
        params_flops_per_token = 2 * n_params
        params_flops_per_seq = params_flops_per_token * self.model.cfg.max_seq_len
        # there are 2 FLOPS per mac; there is A=Q*K^T and out=A*V ops (ie mult by 2)
        attn_flops_per_seq = self.model.cfg.n_layers * 2 * 2 * (self.model.cfg.d_model * (self.model.cfg.max_seq_len ** 2))
        self.__num_fwd_flops =  params_flops_per_seq + attn_flops_per_seq
        return self.__num_fwd_flops
