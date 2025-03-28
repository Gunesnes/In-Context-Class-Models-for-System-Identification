import math
import numpy as np
import time

import torch
import torch.nn as nn
import torch.nn.functional as F

import einops
from einops.layers.torch import Rearrange

import pdb

class PositionalEmbedding(nn.Module):
    """
    Sinusoidal Positional Embedding for sequentiality injection
    """
    def __init__(self, dim):
        super().__init__()
        self.dim = dim
    
    def forward(self, x):
        halfdim = self.dim//2
        emb = math.log(10000) / (halfdim - 1)
        emb = torch.exp(torch.arange(halfdim, device=x.device) * -emb)
        emb = x[:, None] * emb[None, :]
        emb = torch.cat((emb.sin(), emb.cos()), dim=-1)
        return emb

class ContextEncoder(nn.Module):
    """
    Context Encoding for input/output pair enchancement
    """
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self):
        return

class ContextDecoder(nn.Module):    
    """
    Context Decoding for input/output pair enchancement
    """
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self):
        pass

class Clipper(nn.Module):
    """
    Guidance Free DDPM Helper
    """
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self):
        pass

class Downsample(nn.Module):
    """
    Downsampling Unet
    """
    def __init__(self, dim):
        super().__init__()
        self.conv = nn.Conv1d(dim, dim, 3, 2, 1)
    
    def forward(self, x):
        return self.conv(x)

class Upsample(nn.Module):
    """
    Upsampling Unet
    """
    def __init__(self, dim):
        super().__init__()
        self.conv = nn.ConvTranspose1d(dim, dim, 4, 2, 1)
    
    def forward(self, x):
        return self.conv(x)

class ConvolutionBlock(nn.Module):
    """
    Convolution Block pieces that are looped as many times as required for the 
    construction of Unet architecture
    """
    def __init__(self, input, output, kernel_size, ngroups=8):
        super().__init__()
        self.block = nn.Sequential(
            nn.Conv1d(input, output, kernel_size, padding=kernel_size//2),
            Rearrange('batch channels horizon -> batch channels 1 horizon'),
            nn.GroupNorm(ngroups, output),
            Rearrange('batch channels 1 horizon -> batch channels horizon'),
            nn.Mish()
        )

    def forward(self, x):
        return self.block(x)

class LinearAttention(nn.Module):
    """
    Linear Attention Module for Unet
    """
    def __init__(self, dim, heads=4, dim_head=32):
        super().__init__()
        self.scale = dim_head ** -0.5
        self.heads = heads
        hidden_dim = dim_head * heads
        self.to_qkv = nn.Conv1d(dim, hidden_dim * 3, 1, bias=False)
        self.to_out = nn.Conv1d(hidden_dim, dim, 1)

    def forward(self, x):
        qkv = self.to_qkv(x).chunk(3, dim = 1)
        q, k, v = map(lambda t: einops.rearrange(t, 'b (h c) d -> b h c d', h=self.heads), qkv)
        q = q * self.scale

        k = k.softmax(dim = -1)
        context = torch.einsum('b h d n, b h e n -> b h d e', k, v)

        out = torch.einsum('b h d e, b h d n -> b h e n', context, q)
        out = einops.rearrange(out, 'b h c d -> b (h c) d')
        return self.to_out(out)

class ResidualBlock(nn.Module):
    """
    Residual Block for Unet
    """
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, *args, **kwargs):
        return self.fn(x, *args, **kwargs)

class PreNorm(nn.Module):
    """
    Layer normalization on a function - in our case attention
    """
    def __init__(self, dim, fn):
        super().__init__()
        self.fn = fn
        self.norm = LayerNorm(dim)

    def forward(self, x):
        x = self.norm(x)
        return self.fn(x)

class LayerNorm(nn.Module):
    """
    Layer normalization with eps std deviation error
    """
    def __init__(self, dim, eps = 1e-5):
        super().__init__()
        self.eps = eps
        self.g = nn.Parameter(torch.ones(1, dim, 1))
        self.b = nn.Parameter(torch.zeros(1, dim, 1))

    def forward(self, x):
        var = torch.var(x, dim=1, unbiased=False, keepdim=True)
        mean = torch.mean(x, dim=1, keepdim=True)
        return (x - mean) / (var + self.eps).sqrt() * self.g + self.b

class ResidualTemporalBlock(nn.Module):
    """
    Residual Temporal Block for Unet
    """
    def __init__(self, inp_channels, out_channels, embed_dim, horizon, kernel_size=5):
        super().__init__()

        self.blocks = nn.ModuleList([
            ConvolutionBlock(inp_channels, out_channels, kernel_size),
            ConvolutionBlock(out_channels, out_channels, kernel_size),
        ])

        self.time_mlp = nn.Sequential(
            nn.Mish(),
            nn.Linear(embed_dim, out_channels),
            Rearrange('batch t -> batch t 1'),
        )

        self.residual_conv = nn.Conv1d(inp_channels, out_channels, 1) \
            if inp_channels != out_channels else nn.Identity()

    def forward(self, x, t):
        out = self.blocks[0](x) + self.time_mlp(t)
        out = self.blocks[1](out)
        return out + self.residual_conv(x)

class TemporalUnet(nn.Module):
    """
    Unet architecture
    """
    def __init__(
        self,
        horizon,
        transition_dim,
        cond_dim,
        dim=32,
        dim_mults=(1, 2, 4, 8),
        attention=False,
    ):
        super().__init__()

        dims = [transition_dim, *map(lambda m: dim * m, dim_mults)]
        in_out = list(zip(dims[:-1], dims[1:]))
        print(f'[ models/temporal ] Channel dimensions: {in_out}')

        time_dim = dim
        self.time_mlp = nn.Sequential(
            PositionalEmbedding(dim),
            nn.Linear(dim, dim * 4),
            nn.Mish(),
            nn.Linear(dim * 4, dim),
        )

        self.downs = nn.ModuleList([])
        self.ups = nn.ModuleList([])
        num_resolutions = len(in_out)

        print(in_out)
        for ind, (dim_in, dim_out) in enumerate(in_out):
            is_last = ind >= (num_resolutions - 1)

            self.downs.append(nn.ModuleList([
                ResidualTemporalBlock(dim_in, dim_out, embed_dim=time_dim, horizon=horizon),
                ResidualTemporalBlock(dim_out, dim_out, embed_dim=time_dim, horizon=horizon),
                ResidualBlock(PreNorm(dim_out, LinearAttention(dim_out))) if attention else nn.Identity(),
                Downsample(dim_out) if not is_last else nn.Identity()
            ]))

            if not is_last:
                horizon = horizon // 2

        mid_dim = dims[-1]
        self.mid_block1 = ResidualTemporalBlock(mid_dim, mid_dim, embed_dim=time_dim, horizon=horizon)
        self.mid_attn = ResidualBlock(PreNorm(mid_dim, LinearAttention(mid_dim))) if attention else nn.Identity()
        self.mid_block2 = ResidualTemporalBlock(mid_dim, mid_dim, embed_dim=time_dim, horizon=horizon)

        for ind, (dim_in, dim_out) in enumerate(reversed(in_out[1:])):
            is_last = ind >= (num_resolutions - 1)

            self.ups.append(nn.ModuleList([
                ResidualTemporalBlock(dim_out * 2, dim_in, embed_dim=time_dim, horizon=horizon),
                ResidualTemporalBlock(dim_in, dim_in, embed_dim=time_dim, horizon=horizon),
                ResidualBlock(PreNorm(dim_in, LinearAttention(dim_in))) if attention else nn.Identity(),
                Upsample(dim_in) if not is_last else nn.Identity()
            ]))

            if not is_last:
                horizon = horizon * 2

        self.final_conv = nn.Sequential(
            ConvolutionBlock(dim, dim, kernel_size=5),
            nn.Conv1d(dim, transition_dim, 1),
        )

    def forward(self, x, cond, time):
        x = einops.rearrange(x, 'b h t -> b t h')

        t = self.time_mlp(time)
        h = []

        for resnet, resnet2, attn, downsample in self.downs:
            x = resnet(x, t)
            x = resnet2(x, t)
            x = attn(x)
            h.append(x)
            x = downsample(x)

        x = self.mid_block1(x, t)
        x = self.mid_attn(x)
        x = self.mid_block2(x, t)

        for resnet, resnet2, attn, upsample in self.ups:
            x = torch.cat((x, h.pop()), dim=1)
            x = resnet(x, t)
            x = resnet2(x, t)
            x = attn(x)
            x = upsample(x)

        x = self.final_conv(x)

        x = einops.rearrange(x, 'b t h -> b h t')
        return x