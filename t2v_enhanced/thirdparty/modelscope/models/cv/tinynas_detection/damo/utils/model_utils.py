# These codes are copied from modelscope revision c58451baead80d83281f063d12fb377fad415257 
#!/usr/bin/env python3
# Copyright (c) Megvii Inc. All rights reserved.
# Copyright © Alibaba, Inc. and its affiliates.

import math
import time
from copy import deepcopy

import torch
import torch.nn as nn
from thop import profile
from torch.nn.parallel import DistributedDataParallel as DDP

__all__ = [
    'fuse_conv_and_bn',
    'fuse_model',
    'get_model_info',
    'replace_module',
    'ema_model',
]


def ema_scheduler(x, ema_momentum):
    return ema_momentum * (1 - math.exp(-x / 2000))


class ema_model:

    def __init__(self, student, ema_momentum):

        self.model = deepcopy(student).eval()
        self.ema_momentum = ema_momentum
        for param in self.model.parameters():
            param.requires_grad_(False)

    def update(self, iters, student):
        if isinstance(student, DDP):
            student = student.module.state_dict()
        else:
            student = student.state_dict()
        with torch.no_grad():
            momentum = ema_scheduler(iters, self.ema_momentum)
            for name, param in self.model.state_dict().items():
                if param.dtype.is_floating_point:
                    param *= momentum
                    param += (1.0 - momentum) * student[name].detach()


def get_latency(model, inp, iters=500, warmup=2):

    start = time.time()
    for i in range(iters):
        out = model(inp)
        torch.cuda.synchronize()
        if i <= warmup:
            start = time.time()
    latency = (time.time() - start) / (iters - warmup)

    return out, latency


def get_model_info(model, tsize):
    stride = 640
    model = model.eval()
    backbone = model.backbone
    neck = model.neck
    head = model.head
    h, w = tsize
    img = torch.randn((1, 3, stride, stride),
                      device=next(model.parameters()).device)

    bf, bp = profile(deepcopy(backbone), inputs=(img, ), verbose=False)
    bo, bl = get_latency(backbone, img, iters=10)

    nf, np = profile(deepcopy(neck), inputs=(bo, ), verbose=False)
    no, nl = get_latency(neck, bo, iters=10)

    hf, hp = profile(deepcopy(head), inputs=(no, ), verbose=False)
    ho, hl = get_latency(head, no, iters=10)

    _, total_latency = get_latency(model, img)
    total_flops = 0
    info = ''
    for name, flops, params, latency in zip(('backbone', 'neck', 'head'),
                                            (bf, nf, hf), (bp, np, hp),
                                            (bl, nl, hl)):
        params /= 1e6
        flops /= 1e9
        flops *= tsize[0] * tsize[1] / stride / stride * 2  # Gflops
        total_flops += flops
        info += f"{name}'s params(M): {params:.2f}, " + \
                f'flops(G): {flops:.2f}, latency(ms): {latency*1000:.3f}\n'
    info += f'total latency(ms): {total_latency*1000:.3f}, ' + \
            f'total flops(G): {total_flops:.2f}\n'
    return info


def fuse_conv_and_bn(conv, bn):
    # Fuse convolution and batchnorm layers
    # https://tehnokv.com/posts/fusing-batchnorm-and-conv/
    fusedconv = (
        nn.Conv2d(
            conv.in_channels,
            conv.out_channels,
            kernel_size=conv.kernel_size,
            stride=conv.stride,
            padding=conv.padding,
            groups=conv.groups,
            bias=True,
        ).requires_grad_(False).to(conv.weight.device))

    # prepare filters
    w_conv = conv.weight.clone().view(conv.out_channels, -1)
    w_bn = torch.diag(bn.weight.div(torch.sqrt(bn.eps + bn.running_var)))
    fusedconv.weight.copy_(torch.mm(w_bn, w_conv).view(fusedconv.weight.shape))

    # prepare spatial bias
    b_conv = (
        torch.zeros(conv.weight.size(0), device=conv.weight.device)
        if conv.bias is None else conv.bias)
    b_bn = bn.bias - bn.weight.mul(bn.running_mean).div(
        torch.sqrt(bn.running_var + bn.eps))
    fusedconv.bias.copy_(
        torch.mm(w_bn, b_conv.reshape(-1, 1)).reshape(-1) + b_bn)

    return fusedconv


def fuse_model(model):
    from damo.base_models.core.ops import ConvBNAct
    from damo.base_models.backbones.tinynas_res import ConvKXBN

    for m in model.modules():
        if type(m) is ConvBNAct and hasattr(m, 'bn'):
            m.conv = fuse_conv_and_bn(m.conv, m.bn)  # update conv
            delattr(m, 'bn')  # remove batchnorm
            m.forward = m.fuseforward  # update forward
        elif type(m) is ConvKXBN and hasattr(m, 'bn1'):
            m.conv1 = fuse_conv_and_bn(m.conv1, m.bn1)  # update conv
            delattr(m, 'bn1')  # remove batchnorm
            m.forward = m.fuseforward  # update forward

    return model


def replace_module(module,
                   replaced_module_type,
                   new_module_type,
                   replace_func=None):
    """
    Replace given type in module to a new type. mostly used in deploy.

    Args:
        module (nn.Module): model to apply replace operation.
        replaced_module_type (Type): module type to be replaced.
        new_module_type (Type)
        replace_func (function): python function to describe replace logic.
                                 Defalut value None.

    Returns:
        model (nn.Module): module that already been replaced.
    """

    def default_replace_func(replaced_module_type, new_module_type):
        return new_module_type()

    if replace_func is None:
        replace_func = default_replace_func

    model = module
    if isinstance(module, replaced_module_type):
        model = replace_func(replaced_module_type, new_module_type)
    else:  # recurrsively replace
        for name, child in module.named_children():
            new_child = replace_module(child, replaced_module_type,
                                       new_module_type)
            if new_child is not child:  # child is already replaced
                model.add_module(name, new_child)

    return model