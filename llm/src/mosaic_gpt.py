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

from einops import rearrange


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

        self.register_buffer(
            'mask',
            torch.empty((cfg.max_seq_len, cfg.max_seq_len), device=device))
        self.mask_initialized = False
        self.mhsa.out_proj._is_residual = True  # type: ignore

    def _fill_causal_attn_mask(self):
        assert isinstance(self.mask, torch.Tensor)  # for type checking
        torch.full(size=self.mask.shape,
                   fill_value=float('-inf'),
                   out=self.mask)
        torch.triu(input=self.mask, diagonal=1, out=self.mask)

    def forward(self, x, key_padding_mask, attn_mask=None):
        # Two important disclaimers
        # 1. Torch uses additive attention. If your attn_mask/key_padding mask is a float tensor, it will add the floats
        #   directly to your attention matrix. If they are boolean masks, True will be converted to -inf before adding the
        #   mask to your attentions. See https://pytorch.org/docs/stable/generated/torch.nn.MultiheadAttention.html#torch.nn.MultiheadAttention.forward
        #   Basically True/-inf indicates tokens we do not want to attend to.
        #
        # 2. This is is the exact opposite behavior of Huggingface's tokenizers, which use the convention that True denotes tokens
        #   we do want to attend to. See https://huggingface.co/docs/transformers/glossary#attention-mask
        #
        if attn_mask is None:
            if not self.mask_initialized:
                self._fill_causal_attn_mask()
                self.mask_initialized = True
            attn_mask = self.mask
        return self.mhsa(x,
                         x,
                         x,
                         attn_mask=attn_mask,
                         key_padding_mask=~key_padding_mask,
                         need_weights=True)


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
        assert not attn_mask
        return self.mhsa(x,
                         key_padding_mask=key_padding_mask,
                         need_weights=False)


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

        self.alibi = cfg.get('alibi', False)
        self.n_heads = cfg.n_heads
        self.seq_len = cfg.max_seq_len
        if self.alibi:
            self.register_buffer(
                'attn_mask',
                torch.empty((1, self.n_heads, 1, self.seq_len), device=device))
        else:
            self.register_buffer(
                'attn_mask',
                torch.empty((1, 1, 1, self.seq_len), device=device))
        self.attn_mask_initialized = False

    def _fill_attn_mask(self, bias_max: int = 8):
        self.attn_mask.zero_()

        if self.alibi:
            dtype, device = self.attn_mask.dtype, self.attn_mask.device

            alibi_bias = torch.arange(1 - self.seq_len, 1, dtype=dtype, device=device).view(1, 1, 1, self.seq_len)

            m = torch.arange(1, self.n_heads + 1, dtype=dtype, device=device) * bias_max / self.n_heads
            alibi_bias = alibi_bias * (1. / (2 ** m.view(1, self.n_heads, 1, 1)))

            self.attn_mask.add_(alibi_bias)

        self.attn_mask_initialized = True

    def forward(self, x, key_padding_mask=None, attn_mask=None):
        assert key_padding_mask is None
        if attn_mask is None:
            if not self.attn_mask_initialized:
                self._fill_attn_mask()
            attn_mask = self.attn_mask
        return self.mhsa(
            x,
            key_padding_mask=None,
            attn_mask=attn_mask,
            need_weights=False)


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

    def __init__(self, cfg: DictConfig, device: Optional[str] = None):
        super().__init__()
        if cfg.get('alibi', False):
            assert 'triton' in cfg.attn_impl, 'Only triton kernel supports alibi'
        self.ln_1 = nn.LayerNorm(cfg.d_model, device=device)
        if cfg.attn_impl == 'torch':
            self.causal_attn = TorchCausalAttention(cfg, device)
        elif cfg.attn_impl == 'flash':
            self.causal_attn = FlashCausalAttention(cfg, device)
        elif 'triton' in cfg.attn_impl:
            self.causal_attn = TritonFlashCausalAttention(cfg, device)
        else:
            raise ValueError(f'Unknown attn_impl={cfg.attn_impl}')
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
        self.alibi = cfg.get("alibi", None)
        # CogView (https://arxiv.org/abs/2105.13290) and GLM-130B (https://arxiv.org/abs/2210.02414)
        # both report this helping with stabilizing training
        self.embedding_fraction = cfg.get("embedding_fraction", 1)
        assert 0 < self.embedding_fraction <= 1, "model.embedding_fraction must be between 0 (exclusive) and 1 (inclusive)!"

        self.transformer = nn.ModuleDict({'wte': nn.Embedding(cfg.vocab_size, cfg.d_model, device=cfg.device)})
        if not self.alibi:
            self.transformer.update({'wpe': nn.Embedding(cfg.max_seq_len, cfg.d_model, device=cfg.device)})
        self.transformer.update({'emb_drop': nn.Dropout(cfg.emb_pdrop)})
        self.transformer.update({'blocks': nn.ModuleList([
                    GPTBlock(cfg, device=cfg.device)
                    for _ in range(cfg.n_layers)
                ])})
        self.transformer.update({'ln_f': nn.LayerNorm(cfg.d_model, device=cfg.device)})

        if cfg.device != 'meta':
            self.apply(self.param_init_fn)

        self.alibi_bias_max = cfg.get("alibi_bias_max", 8 if self.alibi else None)
        self._attn_mask_initialized = False

        if cfg.attn_impl == 'torch':
            self.register_buffer(
                'attn_mask',
                torch.empty((cfg.max_seq_len, cfg.max_seq_len), device=cfg.device))
        elif cfg.attn_impl == 'flash':
            attn_mask = None
        elif 'triton' in cfg.attn_impl:
            if self.alibi:
                self.register_buffer(
                    'attn_mask',
                    torch.empty((1, cfg.n_heads, 1, cfg.max_seq_len), device=cfg.device))
            else:
                self.register_buffer(
                    'attn_mask',
                    torch.empty((1, 1, 1, cfg.max_seq_len), device=cfg.device))
        else:
            raise ValueError(f'Unknown attn_impl={cfg.attn_impl}')
    
    def _alibi_bias(self, dtype, device, max_s, n_heads, full=False):
        alibi_bias = torch.arange(1 - max_s, 1, dtype=dtype, device=device).view(1, 1, 1, max_s)
        if full:
            alibi_bias = alibi_bias + torch.arange(1 - max_s, 1, dtype=dtype, device=device).view(1, 1, max_s, 1)

        m = torch.arange(1, n_heads + 1, dtype=dtype, device=device) * self.alibi_bias_max / n_heads
        alibi_bias = alibi_bias * (1. / (2 ** m.view(1, n_heads, 1, 1)))
        return alibi_bias

    def _triton_attn_mask(self, x, max_s=None, key_padding_mask=None):
        if self._attn_mask_initialized and key_padding_mask is None:
            return self.attn_mask

        if key_padding_mask is not None and key_padding_mask.bool().logical_not().any():
            dtype, device = x.dtype, x.device

            n, s, d = x.shape
            if max_s:
                max_s = max(s, max_s)
            n_heads = self.cfg.n_heads if self.alibi else 1

            attn_mask = torch.zeros((n, n_heads, max_s, max_s), dtype=dtype, device=device)
            
            m = (~key_padding_mask).reshape(n, 1, 1, max_s)
            m = m + (~key_padding_mask).reshape(n, 1, max_s, 1)
            m[m == 1] = float('-inf')
            attn_mask += m

            if self.alibi:
                alibi_bias = self._alibi_bias(dtype, device, max_s, n_heads, full=True)

                attn_mask.add_(alibi_bias)

            return attn_mask

        # default when no elems are 0 in key_padding_mask
        if self._attn_mask_initialized:
            return self.attn_mask

        self.attn_mask.zero_()

        if self.alibi:
            dtype, device = self.attn_mask.dtype, self.attn_mask.device

            if self.alibi:
                alibi_bias = self._alibi_bias(dtype, device, max_s, self.cfg.n_heads, full=False)

                self.attn_mask.add_(alibi_bias)
        
        self._attn_mask_initialized = True
        
        return self.attn_mask

    def _torch_attn_mask(self, x, max_s=None):
        if self._attn_mask_initialized:
            return self.attn_mask
        
        if not max_s:
            max_s = x.size(1)
        self.attn_mask = torch.full(size=self.attn_mask.shape, fill_value=float('-inf'), out=self.attn_mask)
        torch.triu(input=self.attn_mask, diagonal=1, out=self.attn_mask)

        if self.alibi:
            raise NotImplementedError()
        
        self._attn_mask_initialized = True

        return self.attn_mask

    def forward(self,
                input_ids: torch.LongTensor,
                key_padding_mask: Optional[torch.ByteTensor] = None):
        _, S = input_ids.size()
        assert (
            S <= self.cfg.max_seq_len
        ), f'Cannot forward input with seq_len={S}, this model only supports seq_len<={self.cfg.max_seq_len}'
        pos = torch.arange(0, S, dtype=torch.long,
                           device=input_ids.device).unsqueeze(0)

        tok_emb = self.transformer.wte(input_ids)  # type: ignore

        attn_mask = None
        if self.cfg.attn_impl == 'torch':
            attn_mask = self._torch_attn_mask(tok_emb, max_s=self.cfg.max_seq_len)
        elif self.cfg.attn_impl == 'flash':
            attn_mask = None
        elif 'triton' in self.cfg.attn_impl:
            attn_mask = self._triton_attn_mask(tok_emb, max_s=self.cfg.max_seq_len, key_padding_mask=key_padding_mask)
        else:
            raise ValueError(f'Unknown attn_impl={self.cfgcfg.attn_impl}')

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
            if 'triton' in self.cfg.attn_impl:
                key_padding_mask = None
            x = block(x, key_padding_mask, attn_mask)
        x = self.transformer.ln_f(x)  # type: ignore
        # output embedding weight tied to input embedding
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
