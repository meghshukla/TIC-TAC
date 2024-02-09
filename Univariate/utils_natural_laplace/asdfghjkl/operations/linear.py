import torch
from torch import nn

from .operation import Operation


class Linear(Operation):
    """
    module.weight: f_out x f_in
    module.bias: f_out x 1

    Argument shapes
    in_data: n x f_in
    out_grads: n x f_out
    """
    @staticmethod
    def batch_grads_weight(
        module: nn.Module, in_data: torch.Tensor, out_grads: torch.Tensor
    ):
        batch_grads = torch.matmul(
            out_grads.unsqueeze(-1), in_data.unsqueeze(-2)
        )
        if batch_grads.ndim > 3:
            batch_grads = batch_grads.sum(tuple(range(1, in_data.ndim-1)))
        return batch_grads

    @staticmethod
    def batch_grads_bias(module, out_grads):
        if out_grads.ndim > 2:
            return out_grads.sum(tuple(range(1, out_grads.ndim-1)))
        return out_grads

    @staticmethod
    def batch_grads_kron_weight(
        module: nn.Module, in_data: torch.Tensor, out_grads: torch.Tensor
    ):
        if in_data.ndim > 2:
            in_data = in_data.sum(tuple(range(1, in_data.ndim-1)))
        if out_grads.ndim > 2:
            out_grads = out_grads.mean(tuple(range(1, out_grads.ndim-1)))
        batch_grads = torch.matmul(
            out_grads.unsqueeze(-1), in_data.unsqueeze(-2)
        )
        return batch_grads

    @staticmethod
    def batch_grads_kron_bias(module, out_grads):
        if out_grads.ndim > 2:
            return out_grads.sum(tuple(range(1, out_grads.ndim-1)))
        return out_grads

    @staticmethod
    def cov_diag_weight(module, in_data, out_grads):
        # efficient reduction for augmentation and weight-sharing
        if in_data.ndim > 2:
            in_data = in_data.mean(tuple(range(1, in_data.ndim-1, 1)))
        if out_grads.ndim > 2:
            out_grads = out_grads.sum(tuple(range(1, out_grads.ndim-1, 1)))
        batch_grads = torch.matmul(
            out_grads.unsqueeze(-1), in_data.unsqueeze(-2)
        )
        return batch_grads.square().sum(dim=0)

    @staticmethod
    def cov_diag_bias(module, out_grads):
        if out_grads.ndim > 2:
            out_grads = out_grads.sum(tuple(range(1, out_grads.ndim-1)))
        return out_grads.mul(out_grads).sum(dim=0)

    @staticmethod
    def cov_kron_A(module, in_data):
        if in_data.ndim > 2:
            in_data = in_data.mean(tuple(range(1, in_data.ndim-1, 1)))
        return torch.matmul(in_data.T, in_data)

    @staticmethod
    def cov_kron_B(module, out_grads):
        if out_grads.ndim > 2:
            out_grads = out_grads.sum(tuple(range(1, out_grads.ndim-1, 1)))
        return torch.matmul(out_grads.T, out_grads)

    @staticmethod
    def gram_A(module, in_data1, in_data2):
        if in_data1.ndim > 2:
            in_data1 = in_data1.mean(tuple(range(1, in_data1.ndim-1)))
            in_data2 = in_data2.mean(tuple(range(1, in_data2.ndim-1)))
        return torch.matmul(in_data1, in_data2.T)  # n x n

    @staticmethod
    def gram_B(module, out_grads1, out_grads2):
        if out_grads1.ndim > 2:
            out_grads1 = out_grads1.sum(tuple(range(1, out_grads1.ndim-1)))
            out_grads2 = out_grads2.sum(tuple(range(1, out_grads2.ndim-1)))
        return torch.matmul(out_grads1, out_grads2.T)  # n x n
