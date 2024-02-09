import numpy as np
from typing import List
from functools import partial

import torch
from torch import Tensor
from torch.nn import functional as F
from torch.utils.data import DataLoader, Subset, TensorDataset
import torch.distributed as dist

from .core import extend
from .operations import *
from .precondition import Precondition
from .utils import flatten_after_batch, disable_param_grad


__all__ = [
    'batch',
    'empirical_direct_ntk',
    'empirical_implicit_ntk',
    'empirical_class_wise_direct_ntk',
    'empirical_class_wise_hadamard_ntk',
    'get_preconditioned_kernel_fn',
    'logits_hessian_cross_entropy',
    'natural_gradient_cross_entropy',
    'efficient_natural_gradient_cross_entropy',
    'parallel_efficient_natural_gradient_cross_entropy',
    'kernel_vector_product',
    'kernel_free_cross_entropy',
    'kernel_eigenvalues'
]


_MASTER = 'master'
_ALL = 'all'
_SPLIT = 'split'


def batch(kernel_fn, model, x1, x2=None, batch_size=1, store_on_device=True, is_distributed=False, gather_type=_MASTER):
    """
    :param kernel_fn:
    :param model:
    :param x1:
    :param x2:
    :param batch_size:
    :param store_on_device:
    :param is_distributed:
    :param gather_type:
    :return: Tensor of shape (n, n, c) or (n, n, c, c)
    """

    def _get_loader(x):
        if isinstance(x, DataLoader):
            return x
        elif isinstance(x, Tensor):
            assert x.shape[0] % batch_size == 0, \
                f'data size ({x.shape[0]}) has to be divisible by batch size ({batch_size}).'
            return DataLoader(TensorDataset(x), batch_size)
        else:
            raise ValueError(f'x1 and x2 have to be {DataLoader} or {Tensor}. {type(x)} was given.')

    loader1 = _get_loader(x1)
    if x2 is None:
        loader2 = None
    else:
        loader2 = _get_loader(x2)

    if is_distributed:
        return _parallel(kernel_fn, model, loader1, loader2, store_on_device, gather_type)
    else:
        return _serial(kernel_fn, model, loader1, loader2, store_on_device)


def _get_inputs(data):
    if isinstance(data, (tuple, list)):
        inputs = data[0]
    else:
        inputs = data
    assert isinstance(inputs, torch.Tensor)
    return inputs


def _serial(kernel_fn, model, loader1, loader2=None, store_on_device=True):
    device = next(iter(model.parameters())).device
    tmp_device = device if store_on_device else 'cpu'
    if loader2 is not None:
        rows = []
        for batch1 in loader1:
            batch1 = _get_inputs(batch1).to(device)
            row_kernels = []
            for batch2 in loader2:
                batch2 = _get_inputs(batch2).to(device)
                block = kernel_fn(model, batch1, batch2)
                row_kernels.append(block.to(tmp_device))
            rows.append(torch.cat(row_kernels, dim=1))
    else:
        n_batches1 = len(loader1)
        blocks = [[torch.empty(0) for _ in range(n_batches1)] for _ in range(n_batches1)]
        for i, batch1 in enumerate(loader1):
            batch1 = _get_inputs(batch1).to(device)
            for j, batch2 in enumerate(loader1):
                batch2 = _get_inputs(batch2).to(device)
                if i == j:
                    block = kernel_fn(model, batch1)
                elif i > j:
                    block = blocks[j][i].clone().transpose(0, 1)
                    if block.ndim == 4:
                        # n x n x c x c
                        block = block.transpose(2, 3)
                else:
                    block = kernel_fn(model, batch1, batch2)
                blocks[i][j] = block.to(device)
        rows = [torch.cat(blocks[i], dim=1) for i in range(n_batches1)]

    return torch.cat(rows, dim=0).to(device)


def _get_subset_loader(loader: DataLoader, batch_indices: List):
    batch_size = loader.batch_size
    n_samples = len(loader.dataset)
    subset_sample_indices = []
    for batch_idx in batch_indices:
        start_sample_idx = batch_idx * batch_size
        end_sample_idx = min((batch_idx + 1) * batch_size, n_samples)
        sample_indices = range(start_sample_idx, end_sample_idx)
        subset_sample_indices.extend(sample_indices)
    subset = Subset(loader.dataset, subset_sample_indices)

    return DataLoader(subset,
                      batch_size,
                      pin_memory=loader.pin_memory,
                      num_workers=loader.num_workers)


def _parallel(kernel_fn, model, loader1, loader2=None, store_on_device=True, gather_type=_MASTER):
    device = next(iter(model.parameters())).device
    tmp_device = device if store_on_device else 'cpu'
    assert gather_type in [_MASTER, _ALL, _SPLIT]
    n_batches1 = len(loader1)
    is_symmetric = loader2 is None
    if is_symmetric:
        loader2 = loader1
        n_batches2 = n_batches1
        indices = np.triu_indices(n_batches1)
        indices = [(i, j) for i, j in zip(indices[0], indices[1])]
    else:
        n_batches2 = len(loader2)
        indices = [(i, j) for i in range(n_batches1) for j in range(n_batches2)]

    rank = dist.get_rank()
    is_master = rank == 0
    world_size = dist.get_world_size()
    assert len(indices) >= world_size, f'At least 1 block have to be assigned to each process. There are only {len(indices)} blocks for {world_size} processes.'
    indices_split = np.array_split(indices, world_size)

    local_indices = indices_split[rank]
    subset_loader1 = _get_subset_loader(loader1, [idx[0] for idx in local_indices])
    subset_loader2 = _get_subset_loader(loader2, [idx[1] for idx in local_indices])
    local_blocks = []
    for (i, j), batch1, batch2 in zip(local_indices, subset_loader1, subset_loader2):
        batch1 = _get_inputs(batch1).to(device)
        if i == j and is_symmetric:
            batch2 = None
        else:
            batch2 = _get_inputs(batch2).to(device)
        # bs x bs x c x *
        block = kernel_fn(model, batch1, batch2)
        local_blocks.append(block.to(tmp_device))
    local_blocks = torch.stack(local_blocks).to(device)  # local_n_blocks x bs x bs x c x *

    # match the size of local blocks to the maximum size
    max_n_blocks = len(indices_split[0])
    for _ in range(max_n_blocks - len(local_indices)):
        dummy = torch.zeros_like(local_blocks[0]).unsqueeze(0)
        local_blocks = torch.cat([local_blocks, dummy])

    def _construct_block_matrix(block_list):
        _blocks = [[torch.empty(0) for _ in range(n_batches2)] for _ in range(n_batches1)]
        for _local_blocks, _local_indices in zip(block_list, indices_split):
            for _block, (i, j) in zip(_local_blocks, _local_indices):
                _blocks[i][j] = _block
        if is_symmetric:
            for j in range(n_batches2):
                for i in range(j+1, n_batches1):
                    _block = _blocks[j][i].clone().transpose(0, 1)
                    if _block.ndim == 4:
                        # bs x bs x c x c
                        _block = _block.transpose(2, 3)
                    _blocks[i][j] = _block
        _rows = [torch.cat(_blocks[i], dim=1) for i in range(n_batches1)]
        return torch.cat(_rows, dim=0)  # n x n x c x *

    if gather_type == _MASTER:
        if is_master:
            gather_list = [torch.zeros_like(local_blocks) for _ in range(world_size)]
            dist.gather(local_blocks, gather_list, dst=0)
            return _construct_block_matrix(gather_list)
        else:
            dist.gather(local_blocks, dst=0)
            return None

    elif gather_type == _ALL:
        gather_list = [torch.zeros_like(local_blocks) for _ in range(world_size)]
        dist.all_gather(gather_list, local_blocks)
        return _construct_block_matrix(gather_list)

    assert gather_type == _SPLIT
    assert local_blocks.ndim == 4  # local_n_blocks x bs x bs x c
    n_classes = local_blocks.shape[-1]
    classes_split = np.array_split(range(n_classes), world_size)

    # all-to-all
    gather_list = None
    for dst, local_classes in enumerate(classes_split):
        tensor = local_blocks[:, :, :, local_classes].clone()  # local_n_blocks x bs x bs x local_c
        if rank == dst:
            gather_list = [torch.zeros_like(tensor) for _ in range(world_size)]
            dist.gather(tensor, gather_list, dst=dst)
        else:
            dist.gather(tensor, dst=dst)

    local_c = len(classes_split[rank])
    if local_c > 0:
        local_class_kernels = []
        for k in range(local_c):
            class_block_list = [blocks[:, :, :, k] for blocks in gather_list]
            class_kernel = _construct_block_matrix(class_block_list)
            local_class_kernels.append(class_kernel)

        return torch.stack(local_class_kernels)  # local_c x n x n
    else:
        return None


def linear_network_kernel(model, x, scale, likelihood='classification', 
                          differentiable=False, kron_jac=False):
    operation_name = OP_BATCH_GRADS_KRON if kron_jac else OP_BATCH_GRADS
    n = x.shape
    n_params = sum(p.numel() for p in model.parameters())

    with extend(model, operation_name):
        if x.requires_grad:
            with disable_param_grad(model):
                logits = model(x)
        else:
            logits = model(x)
        if logits.ndim > 2:  # augmented
            logits = logits.mean(dim=1)
        n, c = logits.shape
        j1 = logits.new_zeros(n, c, n_params)
        for k in range(c):
            model.zero_grad()
            scalar = logits[:, k].sum()
            if differentiable:
                scalar.backward(retain_graph=True, create_graph=True)
            else:
                scalar.backward(retain_graph=(k < c - 1))
            j_k = []
            for module in model.modules():
                operation = getattr(module, 'operation', None)
                if operation is None:
                    continue
                batch_grads = operation.get_op_results()[operation_name]
                for g in batch_grads.values():
                    j_k.append(flatten_after_batch(g))
            j_k = torch.cat(j_k, dim=1)  # n x p
            j1[:, k, :] = j_k

    if likelihood == 'classification':
        L = logits_hessian_cross_entropy(logits)  # n x c x c
        j2 = (j1.transpose(1, 2) @ L).transpose(1, 2) * scale  # n x p x c @ n x c x c or for c = 1
    elif likelihood == 'heteroscedastic_regression':
        L = hessian_heteroscedastic_regression(logits)  # n x 2 x 2
        j2 = (j1.transpose(1, 2) @ L).transpose(1, 2) * scale  # n x p x c @ n x c x c or for c = 1
    elif likelihood == 'regression':
        j2 = j1 * scale
    else:
        raise ValueError('Invalid likelihood')
    return logits, torch.einsum('ncp,mdp->nmcd', j1, j2)  # n1 x n1 x c x c


def linear_network_kernel_indep(model, x, scale, likelihood='classification', differentiable=False, 
                                kron_jac=False, single_output=None):
    n = x.shape[0]

    module_list = [[module] * (2 if getattr(module, 'bias', None) is not None else 1)
                   for module in model.modules() if hasattr(module, 'weight')]
    module_list = [(m, loc) for sublist in module_list
                   for m, loc in zip(sublist, ['weight', 'bias'])]
    if len(scale) == 1:
        scale = [scale] * len(module_list)
    assert len(scale) == len(module_list), 'Scale should be either scalar or for each weight and bias.'
    for (module, loc), scalem in zip(module_list, scale):
        setattr(module, f'{loc}_scale', scalem)

    op_name = OP_GRAM_HADAMARD if kron_jac else OP_GRAM_DIRECT
    with extend(model, op_name):
        _zero_kernel(model, n, n)
        if x.requires_grad:
            with disable_param_grad(model):
                outputs = model(x)
        else:
            outputs = model(x)
        if outputs.ndim > 2:  # augmented
            outputs = outputs.mean(dim=1)
        n_classes = outputs.shape[-1]  # c
        if likelihood == 'classification':
            if single_output is None:
                L = logits_diag_hessian_cross_entropy(outputs)  # n x c
            else:
                L = logits_single_hessian_cross_entropy(outputs, single_output)  # n
        elif likelihood == 'heteroscedastic_regression':
            if single_output is None:
                L = hessian_diag_heteroscedastic_regression(outputs)  # n x 2
            else:
                L = hessian_single_heteroscedastic_regression(outputs, single_output)  # n
        else:
            assert likelihood == 'regression'
        kernels = []
        output_range = range(n_classes) if single_output is None else [n_classes]
        for k in output_range:
            model.zero_grad()
            if single_output is None:
                scalar = outputs[:, k].sum()
            elif single_output.ndim == 0:
                scalar = outputs[:, single_output].sum()
            elif single_output.ndim == 1:
                scalar = outputs.gather(1, single_output.unsqueeze(-1)).sum()
            else:
                raise ValueError('Invalid single_output')
            scalar.backward(
                retain_graph=differentiable or (k < n_classes - 1) or (single_output is not None),
                create_graph=differentiable
            )
            if likelihood == 'regression':
                kernels.append(model.kernel)
            else:
                kernels.append(model.kernel * (L if single_output is not None else L[:, k]))
            _zero_kernel(model, n, n)
        _clear_kernel(model)

    for (module, loc), scale in zip(module_list, scale):
        delattr(module, f'{loc}_scale')

    # returns n x c, c x n x n or n x n (for single_output)
    return outputs, kernels[0] if single_output is not None else torch.stack(kernels)


def empirical_network_kernel(model, x, y, lossfunc, scale, differentiable=False, kron_jac=False):
    n = x.shape[0]

    module_list = [[module] * (2 if getattr(module, 'bias', None) is not None else 1)
                   for module in model.modules() if hasattr(module, 'weight')]
    module_list = [(m, loc) for sublist in module_list
                   for m, loc in zip(sublist, ['weight', 'bias'])]
    if len(scale) == 1:
        scale = [scale] * len(module_list)
    assert len(scale) == len(module_list), 'Scale should be either scalar or for each weight and bias.'
    for (module, loc), scalem in zip(module_list, scale):
        setattr(module, f'{loc}_scale', scalem)

    op_name = OP_GRAM_HADAMARD if kron_jac else OP_GRAM_DIRECT
    with extend(model, op_name):
        _zero_kernel(model, n, n)
        if x.requires_grad:
            with disable_param_grad(model):
                outputs = model(x)
        else:
            outputs = model(x)
        if outputs.ndim > 2:  # augmented
            outputs = outputs.mean(dim=1)
        model.zero_grad()
        loss = lossfunc(outputs, y)
        loss.backward(retain_graph=differentiable, create_graph=differentiable)
        kernel = model.kernel
        _clear_kernel(model)

    for (module, loc), scale in zip(module_list, scale):
        delattr(module, f'{loc}_scale')

    return loss, kernel


def empirical_direct_ntk(model, x1, x2=None):
    n1 = x1.shape[0]
    is_single_batch = x2 is None
    if is_single_batch:
        inputs = x1
        n2 = None
    else:
        inputs = torch.cat([x1, x2], dim=0)
        n2 = x2.shape[0]
    n_params = sum(p.numel() for p in model.parameters())

    with extend(model, OP_BATCH_GRADS):
        outputs = model(inputs)
        n_data, n_classes = outputs.shape  # n x c
        j1 = outputs.new_zeros(n1, n_classes, n_params)
        if is_single_batch:
            j2 = None
        else:
            j2 = outputs.new_zeros(n2, n_classes, n_params)
        for k in range(n_classes):
            model.zero_grad()
            scalar = outputs[:, k].sum()
            # scalar.backward(retain_graph=(k < n_classes - 1))
            scalar.backward(retain_graph=True, create_graph=True)
            j_k = []
            for module in model.modules():
                operation = getattr(module, 'operation', None)
                if operation is None:
                    continue
                batch_grads = operation.get_op_results()[OP_BATCH_GRADS]
                for g in batch_grads.values():
                    j_k.append(flatten_after_batch(g))
            j_k = torch.cat(j_k, dim=1)  # n x p
            if is_single_batch:
                j1[:, k, :] = j_k
            else:
                j1[:, k, :] = j_k[:n1]
                j2[:, k, :] = j_k[n1:]

    if is_single_batch:
        return torch.einsum('ncp,mdp->nmcd', j1, j1)  # n1 x n1 x c x c
    else:
        return torch.einsum('ncp,mdp->nmcd', j1, j2)  # n1 x n2 x c x c


def empirical_implicit_ntk(model, x1, x2=None, precond: Precondition = None):
    n1 = x1.shape[0]
    y1 = model(x1)
    n_classes = y1.shape[-1]
    v1 = torch.ones_like(y1).requires_grad_()
    vjp1 = torch.autograd.grad(y1, model.parameters(), v1, create_graph=True)
    vjp1_clone = [v.clone() for v in vjp1]

    if precond is not None:
        # precondition
        precond.precondition_vector(vjp1_clone)

    if x2 is None:
        n2 = n1
        ntk_dot_v = torch.autograd.grad(vjp1, v1, vjp1_clone, create_graph=True)[0]
    else:
        n2 = x2.shape[0]
        y2 = model(x2)
        v2 = torch.ones_like(y2).requires_grad_()
        vjp2 = torch.autograd.grad(y2, model.parameters(), v2, create_graph=True)
        ntk_dot_v = torch.autograd.grad(vjp2, v2, vjp1_clone, create_graph=True)[0]

    print('x req grad', x1.requires_grad)

    ntk = y1.new_zeros(n1, n2, n_classes, n_classes)
    for j in range(n2):
        for k in range(n_classes):
            # retain_graph = j < n2 - 1 or k < n_classes - 1
            retain_graph=True
            kernel = torch.autograd.grad(ntk_dot_v[j][k], v1, retain_graph=retain_graph,
                    create_graph=True)[0]
            ntk[:, j, :, k] = kernel

    return ntk  # n1 x n2 x c x c


def get_preconditioned_kernel_fn(kernel_fn, precond: Precondition):
    return partial(kernel_fn, precond=precond)


def empirical_class_wise_direct_ntk(model, x1, x2=None, precond=None):
    return _empirical_class_wise_ntk(model, x1, x2, hadamard=False, precond=precond)


def empirical_class_wise_hadamard_ntk(model, x1, x2=None, precond=None):
    return _empirical_class_wise_ntk(model, x1, x2, hadamard=True, precond=precond)


def _empirical_class_wise_ntk(model, x1, x2=None, hadamard=False, precond=None):
    if x2 is not None:
        inputs = torch.cat([x1, x2], dim=0)
        n1 = x1.shape[0]
        n2 = x2.shape[0]
    else:
        inputs = x1
        n1 = n2 = x1.shape[0]

    for module in model.modules():
        setattr(module, 'gram_precond', precond)

    op_name = OP_GRAM_HADAMARD if hadamard else OP_GRAM_DIRECT
    with extend(model, op_name):
        _zero_kernel(model, n1, n2)
        outputs = model(inputs)
        n_classes = outputs.shape[-1]  # c
        kernels = []
        for k in range(n_classes):
            model.zero_grad()
            scalar = outputs[:, k].sum()
            scalar.backward(retain_graph=True, create_graph=True)
            kernels.append(model.kernel.clone())
            _zero_kernel(model, n1, n2)
        _clear_kernel(model)

    for module in model.modules():
        delattr(module, 'gram_precond')

    return torch.stack(kernels).permute(1, 2, 0)  # n1 x n2 x c


def logits_hessian_cross_entropy(logits):
    p = F.softmax(logits, dim=-1)
    return torch.diag_embed(p) - torch.bmm(p.unsqueeze(2), p.unsqueeze(1))  # n x c x c


def logits_single_hessian_cross_entropy(logits, single_output):
    if single_output.ndim == 0:
        p = F.softmax(logits, dim=-1)[:, single_output]
    elif single_output.ndim == 1:
        p = F.softmax(logits, dim=-1).gather(1, single_output.unsqueeze(-1)).squeeze(-1)
    else:
        raise ValueError('Invalid single_output')
    return p - torch.square(p)  # n


def logits_diag_hessian_cross_entropy(logits):
    p = F.softmax(logits, dim=-1)
    return p - torch.square(p)  # n x c


def logits_second_order_grad_cross_entropy(logits, targets, damping=1e-5):
    hessian = logits_hessian_cross_entropy(logits)  # n x c x c
    hessian = _add_value_to_diagonal(hessian, damping)

    loss = F.cross_entropy(logits, targets, reduction='sum')
    grads = torch.autograd.grad(loss, logits, retain_graph=True)[0]  # n x c

    return _cholesky_solve(hessian, grads)  # n x c


def hessian_heteroscedastic_regression(logits):
    L = logits.new_zeros((logits.shape[0], 2, 2))
    eta_1, eta_2 = logits[:, 0], logits[:, 1]
    L[:, 0, 0] = - 0.5 / eta_2
    L[:, 0, 1] = L[:, 1, 0] = 0.5 * eta_1 / eta_2.square()
    L[:, 1, 1] = 0.5 / eta_2.square() - 0.5 * eta_1.square() / torch.pow(eta_2, 3)
    return L


def hessian_diag_heteroscedastic_regression(logits):
    L = logits.new_zeros((logits.shape[0], 2))
    eta_1, eta_2 = logits[:, 0], logits[:, 1]
    L[:, 0] = - 0.5 / eta_2
    L[:, 1] = 0.5 / eta_2.square() - 0.5 * eta_1.square() / torch.pow(eta_2, 3)
    return L  # n x 2


def hessian_single_heteroscedastic_regression(logits, single_output):
    L = hessian_diag_heteroscedastic_regression(logits)
    if single_output.ndim == 0:
        return L[:, single_output]
    elif single_output.ndim == 1:
        return L.gather(1, single_output.unsqueeze(-1)).squeeze(-1)
    else:
        raise ValueError('Invalid single_output')


def natural_gradient_cross_entropy(model, inputs, targets, kernel, damping=1e-5):
    outputs = model(inputs)
    n, c = outputs.shape
    hessian = logits_hessian_cross_entropy(outputs)  # n x c x c

    is_class_wise = kernel.ndim == 3  # n x n x c
    mat = outputs.new_zeros(n * c, n * c)  # nc x nc
    for i in range(n):
        for j in range(n):
            if is_class_wise:
                # dense x diagonal
                diag_repeated = kernel[i, j].repeat(c, 1)  # c x c
                block = torch.mul(hessian[i], diag_repeated)
            else:
                # dense x dense
                block = torch.matmul(hessian[i], kernel[i, j])
            mat[i * c: (i+1) * c, j * c: (j+1) * c] = block
    mat.div_(n)
    mat = _add_value_to_diagonal(mat, damping)
    inv = torch.inverse(mat)

    model.zero_grad()
    loss = F.cross_entropy(outputs, targets)
    grads = torch.autograd.grad(loss, outputs, retain_graph=True)[0].flatten()  # nc x 1
    v = torch.matmul(inv, grads).reshape(n, -1)  # n x c

    # compute natural-gradient by auto-differentiation
    torch.autograd.backward(outputs, grad_tensors=v)


def efficient_natural_gradient_cross_entropy(model, inputs, targets, class_kernels, damping=1e-5):
    assert class_kernels.ndim == 3  # c x n x n
    model.zero_grad()
    outputs = model(inputs)

    v = logits_second_order_grad_cross_entropy(outputs, targets, damping)  # n x c

    v = v.transpose(0, 1)  # c x n
    v = _cholesky_solve(class_kernels, v)  # c x n
    v = v.transpose(0, 1)  # n x c

    # compute natural-gradient by auto-differentiation
    torch.autograd.backward(outputs, grad_tensors=v)


def parallel_efficient_natural_gradient_cross_entropy(model, inputs, targets, local_class_kernels, damping=1e-5):
    rank = dist.get_rank()
    world_size = dist.get_world_size()
    local_n = inputs.shape[0]  # n

    # compute second-order gradient w.r.t logits in a data-parallel fashion
    outputs = model(inputs)  # local_n x c
    v = logits_second_order_grad_cross_entropy(outputs, targets, damping)  # local_n x c

    # data to class-parallel (all-to-all)
    n_classes = outputs.shape[-1]  # c
    classes_split = np.array_split(range(n_classes), world_size)
    gather_list = None
    for dst, local_classes in enumerate(classes_split):
        if len(local_classes) == 0:
            break
        tensor = v[:, local_classes].clone()  # local_n x local_c
        if rank == dst:
            gather_list = [torch.zeros_like(tensor) for _ in range(world_size)]
            dist.gather(tensor, gather_list, dst=dst)
        else:
            dist.gather(tensor, dst=dst)

    # solve inverse in a class-parallel fashion
    has_local_classes = len(classes_split[rank]) > 0
    if has_local_classes:
        assert local_class_kernels is not None
        assert local_class_kernels.ndim == 3  # local_c x n x n
        local_c, n, m = local_class_kernels.shape
        assert n == local_n * world_size
        v = torch.cat(gather_list).transpose(0, 1)  # local_c x n
        assert v.shape[0] == local_c and v.shape[1] == n == m, f'rank: {rank}, v: {v.shape}, local_class_kernels: {local_class_kernels.shape}'
        v = _cholesky_solve(local_class_kernels, v)  # local_c x n
    else:
        v = None

    # class to data-parallel (all-to-all)
    gather_list = None
    max_n_classes = len(classes_split[0])
    for dst in range(world_size):
        if has_local_classes:
            tensor = v[:, dst * local_n: (dst + 1) * local_n].clone()  # local_c x local_n
            local_c = len(classes_split[rank])
            for _ in range(max_n_classes - local_c):
                dummy = torch.zeros_like(tensor[0]).unsqueeze(0)
                tensor = torch.cat([tensor, dummy])
        else:
            tensor = inputs.new_zeros(max_n_classes, local_n)
        if rank == dst:
            gather_list = [torch.zeros_like(tensor) for _ in range(world_size)]
            dist.gather(tensor, gather_list, dst=dst)
        else:
            dist.gather(tensor, dst=dst)

    tensors = []
    for tensor, local_classes in zip(gather_list, classes_split):
        local_c = len(local_classes)
        if local_c == 0:
            break
        tensors.append(tensor[:local_c])

    v = torch.cat(tensors).transpose(0, 1)  # local_n x c

    # compute natural-gradient in a data-parallel fashion
    model.zero_grad()
    torch.autograd.backward(outputs, grad_tensors=v)

    # all-reduce natural gradient
    params = [p for p in model.parameters() if p.requires_grad]
    packed_tensor = torch.cat([p.grad.flatten() for p in params])
    dist.all_reduce(packed_tensor)
    pointer = 0
    for p in params:
        numel = p.numel()
        grad = packed_tensor[pointer: pointer + numel].view_as(p.grad)
        p.grad.copy_(grad)
        pointer += numel
    assert pointer == packed_tensor.numel()


def kernel_free_cross_entropy(model,
                              inputs,
                              targets,
                              damping=1e-5,
                              tol=1e-3,
                              max_iters=None,
                              is_distributed=False,
                              print_progress=False):
    outputs = model(inputs)  # n x c
    n_data, n_classes = outputs.shape
    if is_distributed:
        n_data *= dist.get_world_size()
    if max_iters is None:
        max_iters = n_data * n_classes

    hessian = logits_hessian_cross_entropy(outputs)  # n x c x c
    loss = F.cross_entropy(outputs, targets, reduction='sum').div(n_data)
    grads = torch.autograd.grad(loss, outputs, retain_graph=True)[0]  # n x c

    gg = torch.sum(torch.pow(grads, 2))
    if is_distributed:
        dist.all_reduce(gg)
    g_norm = torch.sqrt(gg)

    x = torch.zeros_like(outputs)
    p = grads.clone().requires_grad_(True)
    r = grads.clone()

    last_n = torch.sum(torch.pow(r, 2))
    if is_distributed:
        dist.all_reduce(last_n)
    for i in range(max_iters):
        vjp = torch.autograd.grad(outputs, list(model.parameters()), grad_outputs=p, retain_graph=True, create_graph=True)
        g = [tensor.clone() for tensor in vjp]
        if is_distributed:
            g = _all_reduce_tensor_list(g)
        kernel_vp = torch.autograd.grad(vjp, p, grad_outputs=g)[0]
        u = torch.einsum('nij,nj->ni', hessian, kernel_vp).div(n_data)  # n x c
        u.add_(p, alpha=damping)

        m = torch.sum(p.mul(u))
        if is_distributed:
            dist.all_reduce(m)

        alpha = (last_n / m).item()
        x.add_(p, alpha=alpha)
        r.sub_(u, alpha=alpha)

        n = torch.sum(torch.pow(r, 2))
        if is_distributed:
            dist.all_reduce(n)

        err = n.sqrt() / g_norm
        if print_progress:
            print(f'{i+1}/{max_iters} err={err}')
        if err < tol:
            break
        beta = (n / last_n).item()
        p = r.add(p, alpha=beta)
        last_n = n

    model.zero_grad()
    torch.autograd.backward(outputs, grad_tensors=x)
    if is_distributed:
        params = [p for p in model.parameters() if p.requires_grad]
        packed_tensor = torch.cat([p.grad.flatten() for p in params])
        dist.all_reduce(packed_tensor)
        pointer = 0
        for j, p in enumerate(params):
            numel = p.grad.numel()
            p.grad.copy_(packed_tensor[pointer: pointer + numel].reshape_as(p.grad))
            pointer += numel


def kernel_vector_product(model, inputs, vec):
    outputs = model(inputs)
    vec.requires_grad_(True)
    vjp = torch.autograd.grad(outputs, list(model.parameters()), grad_outputs=vec, create_graph=True)
    return torch.autograd.grad(vjp, vec, grad_outputs=vjp)[0]


def kernel_eigenvalues(model,
                       inputs,
                       top_n=1,
                       max_iters=100,
                       tol=1e-3,
                       eps=1e-6,
                       eigenvectors=False,
                       cross_entropy=False,
                       is_distributed=False,
                       gather_type=_ALL,
                       print_progress=False):
    assert top_n >= 1
    assert max_iters >= 1

    eigvals = []
    eigvecs = []
    outputs = model(inputs)

    if cross_entropy:
        hessian = logits_hessian_cross_entropy(outputs)
    else:
        hessian = None

    for i in range(top_n):
        if print_progress:
            print(f'start power iteration for lambda({i+1}).')
        vec = torch.randn_like(outputs)
        eigval = None
        last_eigval = None
        # power iteration
        for j in range(max_iters):
            # get a vector that is orthogonal to all eigenvalues
            for v in eigvecs:
                alpha = torch.sum(vec.mul(v))
                if is_distributed:
                    dist.all_reduce(alpha)
                vec.sub_(v, alpha=alpha.item())

            # normalize the vector
            vv = torch.pow(vec, 2).sum()
            if is_distributed:
                dist.all_reduce(vv)
            vec.div_(torch.sqrt(vv))

            # J'v
            vec.requires_grad_(True)
            vjp = torch.autograd.grad(outputs, list(model.parameters()), grad_outputs=vec, create_graph=True)
            g = [tensor.clone() for tensor in vjp]
            if is_distributed:
                g = _all_reduce_tensor_list(g)

            # JJ'v
            kernel_vp = torch.autograd.grad(vjp, vec, grad_outputs=g, retain_graph=True)[0]
            if cross_entropy:
                # HJJ'v
                kernel_vp = torch.einsum('nij,nj->ni', hessian, kernel_vp)

            # v'JJ'v / v'v = v'JJ'v
            eigval = torch.sum(kernel_vp.mul(vec))
            if is_distributed:
                dist.all_reduce(eigval)

            if j > 0:
                diff = abs(eigval - last_eigval) / (abs(last_eigval) + eps)
                if print_progress:
                    print(f'{j}/{max_iters} diff={diff}')
                if diff < tol:
                    break

            last_eigval = eigval
            vec = kernel_vp
        eigvals.append(eigval)
        eigvecs.append(vec)

    # sort both in descending order
    eigvals, eigvecs = (list(t) for t in zip(*sorted(zip(eigvals, eigvecs))[::-1]))

    if eigenvectors:
        if is_distributed:
            world_size = dist.get_world_size()
            is_master = dist.get_rank() == 0
            for i, v in enumerate(eigvecs):
                gather_list = [torch.zeros_like(v) for _ in range(world_size)]
                if gather_type == _MASTER:
                    if is_master:
                        dist.gather(v, gather_list, dst=0)
                    else:
                        dist.gather(v, dst=0)
                elif gather_type == _ALL:
                    dist.all_gather(gather_list, v)
                else:
                    raise ValueError(f'Invalid gather type {gather_type}.')
                eigvecs[i] = torch.cat([_v.flatten() for _v in gather_list])
        return eigvals, eigvecs
    else:
        return eigvals


def _all_reduce_tensor_list(tensor_list):
    packed_tensor = torch.cat([tensor.clone().flatten() for tensor in tensor_list])
    dist.all_reduce(packed_tensor)
    pointer = 0
    rst = []
    for i, tensor in enumerate(tensor_list):
        numel = tensor.numel()
        v = packed_tensor[pointer: pointer + numel].clone().reshape_as(tensor)
        rst.append(v)
        pointer += numel

    return rst


def _cholesky_solve(A, b, eps=1e-8):
    A = _add_value_to_diagonal(A, eps)
    if A.ndim > b.ndim:
        b = b.unsqueeze(dim=-1)
    u = torch.cholesky(A)
    return torch.cholesky_solve(b, u).squeeze(dim=-1)


def _add_value_to_diagonal(X, value):
    if X.ndim == 3:
        return torch.stack([_add_value_to_diagonal(X[i], value) for i in range(X.shape[0])])
    else:
        assert X.ndim == 2

    indices = torch.tensor([[i, i] for i in range(X.shape[0])], device=X.device).long()

    values = X.new_ones(X.shape[0]).mul(value)
    return X.index_put(tuple(indices.t()), values, accumulate=True)


def _zero_kernel(model, n_data1, n_data2):
    p = next(iter(model.parameters()))
    kernel = torch.zeros(n_data1,
                         n_data2,
                         device=p.device,
                         dtype=p.dtype)
    setattr(model, 'kernel', kernel)


def _clear_kernel(model):
    if hasattr(model, 'kernel'):
        delattr(model, 'kernel')
